"""라벨링 웹 전용 YOLO v2.6 인증 HTTP inference worker."""

from __future__ import annotations

import hmac
import math
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from gecko_vision_gate.gme_temporal import AnalysisClock

from reporter.gate_lock import acquire_common_gate_lock, release_common_gate_lock
from reporter.gme_worker import _build_runtime_detector

IMAGE_LIMIT = 10 * 1024 * 1024
VIDEO_LIMIT = 50 * 1024 * 1024
MODEL_VERSION = "v2.6-warm-start-s28"
WARNING = "연구용 결과이며 오류 가능"
ALLOWED_MEDIA = {
    "image/jpeg": ("image", ".jpg"),
    "image/png": ("image", ".png"),
    "image/webp": ("image", ".webp"),
    "video/mp4": ("video", ".mp4"),
    "video/webm": ("video", ".webm"),
}


@dataclass(frozen=True, slots=True)
class WorkerDependencies:
    token: str
    detector_factory: Callable[[], object]
    capture_factory: Callable[[str], object]
    acquire_lock: Callable[[], object | None]
    release_lock: Callable[[object | None], None]
    now: Callable[[], datetime]
    temp_root: Path | None = None


class _DetectorHolder:
    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._detector = None
        self._load_lock = threading.Lock()

    def get(self):
        if self._detector is None:
            with self._load_lock:
                if self._detector is None:
                    self._detector = self._factory()
        return self._detector


class _InferenceRejected(RuntimeError):
    pass


def _copy_bounded(source, destination: Path, limit: int) -> int:
    total = 0
    with destination.open("xb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise _InferenceRejected("upload_too_large")
            output.write(chunk)
    if total == 0:
        raise _InferenceRejected("empty_upload")
    return total


def _magic_matches(path: Path, content_type: str) -> bool:
    head = path.read_bytes()[:16]
    if content_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if content_type == "video/mp4":
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if content_type == "video/webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _normalized_detections(detector, frame, timestamp_sec: float) -> list[dict]:
    height, width = frame.shape[:2]
    rows = []
    for detection in detector.detect(frame, timestamp_sec):
        center_x, center_y, box_width, box_height = detection.bbox_xywh
        x1 = max(0.0, center_x - box_width / 2)
        y1 = max(0.0, center_y - box_height / 2)
        x2 = min(float(width), center_x + box_width / 2)
        y2 = min(float(height), center_y + box_height / 2)
        if x2 <= x1 or y2 <= y1:
            continue
        rows.append(
            {
                "label": "gecko",
                "confidence": detection.confidence,
                "bbox": {
                    "x": x1 / width,
                    "y": y1 / height,
                    "width": (x2 - x1) / width,
                    "height": (y2 - y1) / height,
                },
            }
        )
    return rows


def _analyze_image(path: Path, detector) -> list[dict]:
    frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if frame is None:
        raise _InferenceRejected("image_decode_failed")
    height, width = frame.shape[:2]
    if width <= 0 or height <= 0 or width * height > 20_000_000:
        raise _InferenceRejected("image_dimensions_invalid")
    return [{"frame_index": 0, "timestamp_ms": 0.0, "detections": _normalized_detections(detector, frame, 0.0)}]


def _analyze_video(path: Path, detector, capture_factory) -> list[dict]:
    cap = capture_factory(str(path))
    frames = []
    try:
        if not cap.isOpened():
            raise _InferenceRejected("video_decode_failed")
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        width = int(round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)))
        height = int(round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not math.isfinite(fps) or not 0 < fps <= 30:
            raise _InferenceRejected("video_fps_invalid")
        if width <= 0 or height <= 0 or width > 1920 or height > 1080:
            raise _InferenceRejected("video_dimensions_invalid")
        if math.isfinite(frame_count) and frame_count > 0 and frame_count / fps > 60.0 + 1e-9:
            raise _InferenceRejected("video_duration_invalid")
        clock = AnalysisClock(max_analysis_fps=10.0)
        decoded = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_index = decoded
            decoded += 1
            timestamp_sec = frame_index / fps
            if timestamp_sec > 60.0 + 1e-9:
                raise _InferenceRejected("video_duration_invalid")
            if clock.accept(frame_index, fps):
                frames.append(
                    {
                        "frame_index": frame_index,
                        "timestamp_ms": timestamp_sec * 1000,
                        "detections": _normalized_detections(detector, frame, timestamp_sec),
                    }
                )
        if decoded == 0 or not frames:
            raise _InferenceRejected("video_decode_failed")
        return frames
    finally:
        cap.release()


def _infer(path: Path, media_kind: str, holder: _DetectorHolder, deps: WorkerDependencies) -> list[dict]:
    lock = deps.acquire_lock()
    if lock is None:
        raise _InferenceRejected("detector_busy")
    try:
        detector = holder.get()
        if getattr(detector, "model_version", None) != MODEL_VERSION:
            raise _InferenceRejected("model_version_mismatch")
        if media_kind == "image":
            return _analyze_image(path, detector)
        return _analyze_video(path, detector, deps.capture_factory)
    finally:
        deps.release_lock(lock)


def create_app(deps: WorkerDependencies) -> FastAPI:
    app = FastAPI()
    holder = _DetectorHolder(deps.detector_factory)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "model_version": MODEL_VERSION}

    @app.post("/v1/infer")
    async def infer(request: Request):
        if not deps.token:
            raise HTTPException(status_code=503, detail="inference unavailable")
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {deps.token}"
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="invalid multipart") from exc
        if set(form.keys()) != {"media", "request_id", "training_consent"}:
            raise HTTPException(status_code=400, detail="invalid fields")
        if any(len(form.getlist(name)) != 1 for name in ("media", "request_id", "training_consent")):
            raise HTTPException(status_code=400, detail="invalid fields")
        upload = form.get("media")
        request_id = form.get("request_id")
        consent = form.get("training_consent")
        if not isinstance(upload, UploadFile) or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise HTTPException(status_code=400, detail="invalid request")
        if consent not in {"true", "false"}:
            raise HTTPException(status_code=400, detail="invalid consent")
        media = ALLOWED_MEDIA.get(upload.content_type or "")
        if media is None:
            raise HTTPException(status_code=415, detail="unsupported media")
        media_kind, suffix = media
        limit = IMAGE_LIMIT if media_kind == "image" else VIDEO_LIMIT
        try:
            with tempfile.TemporaryDirectory(dir=deps.temp_root) as directory:
                path = Path(directory) / f"source{suffix}"
                await run_in_threadpool(_copy_bounded, upload.file, path, limit)
                if not await run_in_threadpool(_magic_matches, path, upload.content_type):
                    raise HTTPException(status_code=415, detail="media signature mismatch")
                frames = await run_in_threadpool(_infer, path, media_kind, holder, deps)
        except HTTPException:
            raise
        except _InferenceRejected as exc:
            status = 413 if str(exc) == "upload_too_large" else 503
            raise HTTPException(status_code=status, detail="inference unavailable") from None
        finally:
            await upload.close()
        return {
            "request_id": request_id,
            "media_kind": media_kind,
            "model_version": MODEL_VERSION,
            "provider_mode": "worker",
            "processed_at": deps.now().astimezone(timezone.utc).isoformat(),
            "warning": WARNING,
            "frames": frames,
            "contribution_status": "candidate_only" if consent == "true" else "not_requested",
        }

    return app


def _production_detector():
    detector, _provenance = _build_runtime_detector()
    return detector


app = create_app(
    WorkerDependencies(
        token=os.environ.get("YOLO_HTTP_WORKER_TOKEN", ""),
        detector_factory=_production_detector,
        capture_factory=cv2.VideoCapture,
        acquire_lock=acquire_common_gate_lock,
        release_lock=release_common_gate_lock,
        now=lambda: datetime.now(timezone.utc),
    )
)
