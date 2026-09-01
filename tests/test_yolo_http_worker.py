from __future__ import annotations

import warnings
from datetime import datetime, timezone

import cv2
import numpy as np
from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

from starlette.testclient import TestClient

from gecko_vision_gate.gme_contracts import Detection
from reporter import gate_lock
from reporter import yolo_http_worker as worker
from reporter.yolo_http_worker import WorkerDependencies, create_app


class FakeDetector:
    model_version = "v2.6-warm-start-s28"
    bbox_coordinate_contract = "xywh-top-left-v1"

    def __init__(self, detections=()):
        self.detections = detections
        self.calls = []

    def detect(self, frame, timestamp_sec):
        self.calls.append((frame.shape, timestamp_sec))
        return tuple(
            Detection(timestamp_sec, bbox, confidence, "gecko")
            for bbox, confidence in self.detections
        )


class FakeCapture:
    def __init__(self, frames, fps):
        self.frames = list(frames)
        self.fps = fps
        self.index = 0
        self.released = False

    def isOpened(self):
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return self.frames[0].shape[1]
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return self.frames[0].shape[0]
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return len(self.frames)
        return 0

    def read(self):
        if self.index >= len(self.frames):
            return False, None
        frame = self.frames[self.index]
        self.index += 1
        return True, frame

    def release(self):
        self.released = True


def _jpeg(width=100, height=100):
    ok, encoded = cv2.imencode(".jpg", np.zeros((height, width, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def _deps(tmp_path, detector, *, capture_factory=cv2.VideoCapture):
    return WorkerDependencies(
        token="worker-token",
        detector_factory=lambda: detector,
        capture_factory=capture_factory,
        acquire_lock=lambda: object(),
        release_lock=lambda _lock: None,
        now=lambda: datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        temp_root=tmp_path,
    )


def test_infer_rejects_missing_worker_token(tmp_path):
    client = TestClient(create_app(_deps(tmp_path, FakeDetector())))

    response = client.post(
        "/v1/infer",
        data={"request_id": "req-1", "training_consent": "false"},
        files={"media": ("x.jpg", _jpeg(), "image/jpeg")},
    )

    assert response.status_code == 401
    assert list(tmp_path.iterdir()) == []


def test_image_infer_preserves_top_left_v26_boxes(tmp_path):
    detector = FakeDetector(([(25.0, 40.0, 30.0, 40.0), 0.91],))
    client = TestClient(create_app(_deps(tmp_path, detector)))

    response = client.post(
        "/v1/infer",
        headers={"Authorization": "Bearer worker-token"},
        data={"request_id": "req-1", "training_consent": "false"},
        files={"media": ("x.jpg", _jpeg(), "image/jpeg")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == "req-1"
    assert body["media_kind"] == "image"
    assert body["model_version"] == "v2.6-warm-start-s28"
    assert body["provider_mode"] == "worker"
    assert body["frames"] == [
        {
            "frame_index": 0,
            "timestamp_ms": 0.0,
            "detections": [
                {
                    "label": "gecko",
                    "confidence": 0.91,
                    "bbox": {"x": 0.25, "y": 0.4, "width": 0.3, "height": 0.4},
                }
            ],
        }
    ]
    assert body["contribution_status"] == "not_requested"
    assert list(tmp_path.iterdir()) == []


def test_infer_rejects_detector_coordinate_contract_mismatch(tmp_path):
    detector = FakeDetector()
    detector.bbox_coordinate_contract = "xywh-center-v1"
    client = TestClient(create_app(_deps(tmp_path, detector)))

    response = client.post(
        "/v1/infer",
        headers={"Authorization": "Bearer worker-token"},
        data={"request_id": "req-contract", "training_consent": "false"},
        files={"media": ("x.jpg", _jpeg(), "image/jpeg")},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "inference unavailable"}
    assert detector.calls == []
    assert list(tmp_path.iterdir()) == []


def test_video_decode_uses_at_most_10fps_and_releases_capture(tmp_path):
    frames = [np.zeros((80, 100, 3), dtype=np.uint8) for _ in range(26)]
    capture = FakeCapture(frames, 25.0)
    detector = FakeDetector()
    client = TestClient(create_app(_deps(tmp_path, detector, capture_factory=lambda _path: capture)))

    response = client.post(
        "/v1/infer",
        headers={"Authorization": "Bearer worker-token"},
        data={"request_id": "req-video", "training_consent": "true"},
        files={"media": ("x.mp4", b"\x00\x00\x00\x18ftypisom" + b"0" * 64, "video/mp4")},
    )

    assert response.status_code == 200, response.text
    assert [row["frame_index"] for row in response.json()["frames"]] == [0, 3, 5, 8, 10, 13, 15, 18, 20, 23, 25]
    assert [row[1] for row in detector.calls] == [0.0, 0.12, 0.2, 0.32, 0.4, 0.52, 0.6, 0.72, 0.8, 0.92, 1.0]
    assert response.json()["contribution_status"] == "candidate_only"
    assert capture.released is True
    assert list(tmp_path.iterdir()) == []


def test_production_inference_waits_for_current_gme_batch(monkeypatch):
    lock = object()
    attempts = []

    def acquire(_path):
        attempts.append(1)
        return lock if len(attempts) == 3 else None

    class Clock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += seconds

    monkeypatch.setattr(gate_lock, "acquire_common_gate_lock", acquire)
    monkeypatch.setattr(gate_lock, "time", Clock, raising=False)

    assert worker._acquire_public_inference_lock() is lock
    assert len(attempts) == 3
    assert Clock.now > 0


def test_public_inference_lock_wait_is_bounded(monkeypatch):
    attempts = []

    class Clock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += seconds

    monkeypatch.setattr(
        gate_lock,
        "acquire_common_gate_lock",
        lambda _path: attempts.append(1),
    )
    monkeypatch.setattr(gate_lock, "time", Clock)

    assert gate_lock.wait_for_common_gate_lock(
        timeout_sec=0.25,
        poll_interval_sec=0.1,
    ) is None
    assert len(attempts) == 4
    assert Clock.now == 0.25
