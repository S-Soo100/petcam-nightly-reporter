"""Mac mini Gecko Motion Engine one-shot production shadow worker."""

from __future__ import annotations

import socket
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from supabase import create_client

from gecko_vision_gate.gme_contracts import GMEConfig
from gecko_vision_gate.gme_detector import build_detector
from gecko_vision_gate.gme_engine import analyze_clip, detector_identity
from gecko_vision_gate.gme_serialization import serialize_artifacts
from gecko_vision_gate.gme_yolo_detector import build_yolo_detector

from reporter import config, r2
from reporter.gate_lock import acquire_common_gate_lock, release_common_gate_lock
from reporter.gme_artifacts import ArtifactUploadError, upload_artifacts
from reporter.gme_runtime_policy import allow_historical_claim
from reporter.gme_store import (
    GMEStoreError, StaleGMEJobError, claim_jobs, complete_job, fail_job, insert_run,
    load_clip_r2_keys, operational_stats,
)
from reporter.r2 import R2AccessDenied, R2SourceMissing
from reporter.vlm_host_guard import HostOwnershipError, require_expected_host

V26_CHECKPOINT_SHA256 = "a00e5a7a1e1f9197accb036339a38a7c821f03c8ab79611ebce89e5cde59b513"
V26_DETECTOR_FREEZE_SHA256 = "8f8e02beb452ec2ddfdce344dff507294f56136c69224990c50552d22bb343a0"
V26_DETECTOR_IDENTITY = "89e4738a60ebb71900e05e96f5b7262e8b900f5c9bba9b9cb9e34fca36f789b7"


@dataclass(frozen=True, slots=True)
class Producer:
    host: str
    run_id: str
    code_ref: str


class _JobFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _run_payload(job, analysis, uploaded, producer: Producer, *, detector_provenance: dict) -> dict:
    return {
        "clip_id": job.clip_id, "job_id": job.id,
        "engine_schema_version": job.engine_schema_version,
        "algorithm_version": job.algorithm_version, "detector_identity": job.detector_identity,
        "detector_provenance": dict(detector_provenance),
        "tracker_provenance": {"implementation": "opencv-sparse-lk-v0"},
        "engine_provenance": {"schema": job.engine_schema_version, "algorithm": job.algorithm_version},
        "producer_host": producer.host, "producer_run_id": producer.run_id,
        "producer_code_ref": producer.code_ref, "status": analysis.status,
        "duration_sec": analysis.duration_sec, "decoded_frame_count": analysis.decoded_frame_count,
        "analyzed_frame_count": analysis.analyzed_frame_count, "source_fps": analysis.source_fps,
        "candidate_moving_sec_any_gecko": analysis.candidate_moving_sec_any_gecko,
        "moving_gecko_seconds": analysis.moving_gecko_seconds, "visible_sec": analysis.visible_sec,
        "unknown_sec": analysis.unknown_sec, "camera_motion_sec": analysis.camera_motion_sec,
        "max_simultaneous_geckos": analysis.max_simultaneous_geckos,
        "state_intervals": [asdict(v) for v in analysis.intervals],
        "tracking_quality": asdict(analysis.tracking_quality),
        "permanent_artifact_key": uploaded.permanent_key,
        "permanent_artifact_sha256": uploaded.permanent_sha256,
        "permanent_artifact_bytes": uploaded.permanent_bytes,
        "debug_artifact_key": uploaded.debug_key, "debug_artifact_sha256": uploaded.debug_sha256,
        "debug_artifact_bytes": uploaded.debug_bytes,
    }


def _safe_fail(sb, job, failure, worker_host, now, fail_fn):
    try:
        fail_fn(sb, job_id=job.id, failure_code=failure.code, retryable=failure.retryable,
                worker_host=worker_host, now=now)
    except Exception as exc:  # noqa: BLE001 - lease/DB 원문 비노출
        print(f"[gme] fail-record error job={job.id[:8]} type={type(exc).__name__}", file=sys.stderr)


def _build_runtime_detector():
    if config.GME_DETECTOR_BACKEND == "yolo26n":
        detector = build_yolo_detector(
            checkpoint=config.GME_CHECKPOINT_PATH,
            expected_sha256=config.GME_CHECKPOINT_SHA256,
            model_version=config.GME_MODEL_VERSION,
            raw_confidence=config.GME_RAW_CONFIDENCE,
            score_threshold=config.GME_SCORE_THRESHOLD,
            image_size=config.GME_IMAGE_SIZE,
            nms_iou=config.GME_NMS_IOU,
            post_nms_iou=config.GME_POST_NMS_IOU,
            max_detections=config.GME_MAX_DETECTIONS,
            analysis_fps=config.GME_ANALYSIS_FPS,
            temporal_window_frames=config.GME_TEMPORAL_WINDOW_FRAMES,
            temporal_min_positive_frames=config.GME_TEMPORAL_MIN_POSITIVE_FRAMES,
            device=config.GME_DEVICE,
        )
        provenance = {
            "model_name": detector.model_name,
            "model_version": detector.model_version,
            "checkpoint_sha256": detector.checkpoint_sha256,
            "detector_freeze_sha256": config.GME_DETECTOR_FREEZE_SHA256,
            "detector_identity": detector_identity(detector),
            "raw_confidence": detector.raw_confidence,
            "threshold": detector.threshold,
            "image_size": detector.image_size,
            "model_nms_iou": detector.nms_iou,
            "post_nms_iou": detector.post_nms_iou,
            "max_detections": detector.max_detections,
            "analysis_fps": config.GME_ANALYSIS_FPS,
            "temporal_window_frames": config.GME_TEMPORAL_WINDOW_FRAMES,
            "temporal_min_positive_frames": config.GME_TEMPORAL_MIN_POSITIVE_FRAMES,
        }
        return detector, provenance
    if config.GME_DETECTOR_BACKEND == "rfdetr":
        detector = build_detector(
            checkpoint=config.GME_CHECKPOINT_PATH,
            threshold=config.GME_GATE_THRESHOLD,
        )
        provenance = {
            "model_name": detector.model_name,
            "model_version": detector.model_version,
            "checkpoint_sha256": detector.checkpoint_sha256,
            "detector_identity": detector_identity(detector),
            "threshold": detector.threshold,
        }
        return detector, provenance
    raise ValueError("unsupported detector backend")


def _validated_v26_engine_config() -> GMEConfig:
    """DB/R2 접근 전에 승인된 v2.6 실행값과 로컬 Gate 계약을 함께 검증한다."""

    expected = {
        "GME_DETECTOR_BACKEND": "yolo26n",
        "GME_CHECKPOINT_SHA256": V26_CHECKPOINT_SHA256,
        "GME_DETECTOR_FREEZE_SHA256": V26_DETECTOR_FREEZE_SHA256,
        "GME_DETECTOR_IDENTITY": V26_DETECTOR_IDENTITY,
        "GME_MODEL_VERSION": "v2.6-warm-start-s28",
        "GME_RAW_CONFIDENCE": 0.001,
        "GME_SCORE_THRESHOLD": 0.15,
        "GME_IMAGE_SIZE": 960,
        "GME_NMS_IOU": 0.70,
        "GME_POST_NMS_IOU": 0.55,
        "GME_MAX_DETECTIONS": 50,
        "GME_ANALYSIS_FPS": 10.0,
        "GME_ANCHOR_INTERVAL_SEC": 0.1,
        "GME_TEMPORAL_WINDOW_FRAMES": 5,
        "GME_TEMPORAL_MIN_POSITIVE_FRAMES": 3,
        "GME_DEVICE": "mps",
    }
    for name, value in expected.items():
        if getattr(config, name) != value:
            raise ValueError(f"v2.6 contract mismatch: {name}")
    engine_config = GMEConfig.v26()
    if (
        engine_config.analysis_fps != config.GME_ANALYSIS_FPS
        or engine_config.anchor_interval_sec != config.GME_ANCHOR_INTERVAL_SEC
        or engine_config.detection_window_frames != config.GME_TEMPORAL_WINDOW_FRAMES
        or engine_config.detection_min_positive_frames != config.GME_TEMPORAL_MIN_POSITIVE_FRAMES
        or not engine_config.detector_every_analysis_frame
    ):
        raise ValueError("local Gate v2.6 engine contract mismatch")
    return engine_config


def process_jobs(
    sb, jobs, clip_keys, *, worker_host: str, now: datetime, temp_root: Path | None,
    download_fn, analyze_fn, serialize_fn, upload_fn, insert_fn, complete_fn, fail_fn,
    producer: Producer,
    detector_provenance: dict,
) -> dict:
    stats = {"jobs": len(jobs), "succeeded": 0, "failed": 0, "terminal": 0, "stale": 0}
    for job in jobs:
        try:
            if job.detector_identity != detector_provenance.get("detector_identity"):
                raise _JobFailure("invalid_metadata", retryable=False)
            key = clip_keys.get(job.clip_id)
            if not key:
                raise _JobFailure("invalid_metadata", retryable=False)
            with tempfile.TemporaryDirectory(dir=temp_root) as directory:
                destination = Path(directory) / "source.mp4"
                try:
                    download_fn(key, destination)
                except R2SourceMissing as exc:
                    raise _JobFailure("source_media_missing", retryable=False) from exc
                except R2AccessDenied as exc:
                    raise _JobFailure("r2_access_denied", retryable=False) from exc
                except Exception as exc:  # noqa: BLE001
                    raise _JobFailure("r2_download_failed", retryable=True) from exc
                try:
                    analysis = analyze_fn(destination)
                except Exception as exc:  # noqa: BLE001
                    raise _JobFailure("gme_compute_failed", retryable=True) from exc
                if analysis.status != "ok":
                    code = "decode_no_frames" if analysis.status == "no_decodable_frames" else "invalid_metadata"
                    raise _JobFailure(code, retryable=False)
                if analysis.artifact_identity.detector_identity != job.detector_identity:
                    raise _JobFailure("gme_compute_failed", retryable=False)
                serialized = serialize_fn(analysis)
                try:
                    uploaded = upload_fn(
                        clip_id=job.clip_id, run_identity=serialized.permanent_sha256,
                        permanent=serialized.permanent_gzip, debug=serialized.debug_gzip,
                    )
                except ArtifactUploadError as exc:
                    raise _JobFailure("artifact_upload_failed", retryable=True) from exc
                try:
                    run_row = insert_fn(
                        sb,
                        _run_payload(
                            job, analysis, uploaded, producer,
                            detector_provenance=detector_provenance,
                        ),
                    )
                    complete_fn(sb, job_id=job.id, run_id=run_row["id"], worker_host=worker_host)
                except StaleGMEJobError:
                    stats["stale"] += 1
                    continue
                except GMEStoreError as exc:
                    raise _JobFailure("db_transient", retryable=True) from exc
            stats["succeeded"] += 1
        except _JobFailure as failure:
            _safe_fail(sb, job, failure, worker_host, now, fail_fn)
            stats["failed"] += 1
            if not failure.retryable:
                stats["terminal"] += 1
        except Exception as exc:  # noqa: BLE001
            failure = _JobFailure("internal_error", retryable=True)
            _safe_fail(sb, job, failure, worker_host, now, fail_fn)
            stats["failed"] += 1
            print(f"[gme] unexpected job={job.id[:8]} type={type(exc).__name__}", file=sys.stderr)
    return stats


def run(
    *, sb_factory=None, now: datetime | None = None, hostname_fn=socket.gethostname,
    acquire_lock_fn=acquire_common_gate_lock, release_lock_fn=release_common_gate_lock,
) -> int:
    if not config.GME_ENABLED:
        print("[gme] disabled — skip")
        return 0
    host = hostname_fn()
    try:
        require_expected_host(host, config.GME_EXPECTED_HOST)
    except HostOwnershipError as exc:
        print(f"[gme] host guard fail-closed: {exc}")
        return 2
    lock_fd = acquire_lock_fn()
    if lock_fd is None:
        print("[gme] common Gate lock busy — skip")
        return 0
    try:
        now = now or datetime.now(timezone.utc)
        try:
            engine_config = _validated_v26_engine_config()
        except ValueError as exc:
            print(f"[gme] {exc}", file=sys.stderr)
            return 2
        sb_factory = sb_factory or (lambda: create_client(config.SUPABASE_URL, config.SUPABASE_KEY))
        sb = sb_factory()
        queue_stats = operational_stats(sb, now=now)
        include_historical = allow_historical_claim(queue_stats, max_live_lag_sec=config.GME_MAX_LIVE_LAG_SEC)
        jobs = claim_jobs(sb, limit=config.GME_BATCH_LIMIT, worker_host=host, now=now,
                          include_historical=include_historical,
                          detector_identity=config.GME_DETECTOR_IDENTITY)
        if not jobs:
            print("[gme] no jobs — skip")
            return 0
        clip_keys = load_clip_r2_keys(sb, [job.clip_id for job in jobs])
        detector, detector_provenance = _build_runtime_detector()
        if detector_provenance.get("detector_identity") != config.GME_DETECTOR_IDENTITY:
            print("[gme] detector execution identity mismatch", file=sys.stderr)
            return 2
        producer = Producer(host, now.strftime("%Y%m%dT%H%M%S"), "gme-worker/gme-motion-v0")
        stats = process_jobs(
            sb, jobs, clip_keys, worker_host=host, now=now, temp_root=None,
            download_fn=r2.download_clip,
            analyze_fn=lambda path: analyze_clip(path, detector=detector, config=engine_config),
            serialize_fn=serialize_artifacts,
            upload_fn=lambda **kwargs: upload_artifacts(r2.get_r2_client(), bucket=config.R2_BUCKET, **kwargs),
            insert_fn=insert_run, complete_fn=complete_job, fail_fn=fail_job, producer=producer,
            detector_provenance=detector_provenance,
        )
        print(f"[gme] jobs={stats['jobs']} ok={stats['succeeded']} fail={stats['failed']} terminal={stats['terminal']}")
        return 1 if stats["failed"] else 0
    finally:
        release_lock_fn(lock_fd)


if __name__ == "__main__":
    raise SystemExit(run())
