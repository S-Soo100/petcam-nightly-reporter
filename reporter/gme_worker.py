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
from gecko_vision_gate.gme_engine import (
    ENGINE_SCHEMA_VERSION,
    algorithm_version_for,
    analyze_clip,
    detector_identity,
)
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
V26_DETECTOR_IDENTITY = "deccfc8315d3c00edb5bf59db3c573dca568e9d6d7a5da8d7dc93d2082bdb899"
# v2.6.1 승인 identity — threshold 가 detector identity 해시에 들어가므로 기준별로 따로 승인한다.
V261_IDENTITY_BY_THRESHOLD = {
    0.30: "44dd382cba74355c98eff3c202acf1b1195d92870c50ac86f4fafa4c75aed92e",
    0.15: "81688d0a5ae61299e569573bbc7429bbe362ef9f086cd1cf3e699f9c18c5de2c",  # 2026-09-23 gme-motion-v2 전환(owner)
}
BETA_V2_MODEL_VERSION = "v2.6.1-beta-v2-s27"
BETA_V2_CHECKPOINT_SHA256 = "91a275ad1a047e7a0f70a8a9c1212da7d2b21532bb568a33b3401be9e338751d"
BETA_V2_DETECTOR_FREEZE_SHA256 = "74d2dc854198b8f871005d591dc18bd5273d7de857ebcea11b4d32f60343c75f"
BETA_V2_DETECTOR_IDENTITY = "8968c0546e4a86486eb56fb1ab89939da9aedabe3306131ffe8f052669ee0964"
# beta v3(2026-10-19 owner 채택): beta v2 + 움직이는 순간·미검출 GT 1,111 warm-start. 컷 0.70(재판정 v3b 선택).
# freeze = selection-v3b.json, identity = 운영 런타임(gate 9968d29)에서 M4·home-mac 양쪽 계산값 일치.
BETA_V3_MODEL_VERSION = "v2.6.1-beta-v3-s27"
BETA_V3_CHECKPOINT_SHA256 = "f1046788e179f40e0e8a72af650848af3fa7b934c08e54e28a77930dccc0fb5e"
BETA_V3_DETECTOR_FREEZE_SHA256 = "430c7f0485acd406fb8415603e8a135ab67213bb0a101cbb6bab0127fd5bd57c"
BETA_V3_DETECTOR_IDENTITY = "83a36f6378aebc6e911deebc003d5307625703b33111babc2dd6d57dc502ff9e"
# beta v4(2026-10-23 owner 운영 채택, 시험지 판정 hold — G2 소품 오탐 3장): beta v3 + 새 카메라 놓친 프레임 375×3 warm-start.
# 컷 0.70 은 시험지 고정값. freeze = petcam-lab experiments/gme-detector-eval-suite-v1/freeze-v4.json,
# identity = 운영 런타임(gate 9968d29)에서 M1·M4·맥북 계산값 일치.
BETA_V4_MODEL_VERSION = "v2.6.1-beta-v4-s27"
BETA_V4_CHECKPOINT_SHA256 = "42b33c349cefcdfd241138630d4b617c50d70a43571cde7dd497cfd97d5fb3ad"
BETA_V4_DETECTOR_FREEZE_SHA256 = "9836364febeeef3b468427dfe1411b32bdefb4181052a91e5e2d9c750e17f5c7"
BETA_V4_DETECTOR_IDENTITY = "66d13f99571feb8d850436b447cd7e78256076def145c6adaa4ec7c969e5e897"
# 알고리즘 버전 → 엔진 설정. 여기 없는 버전은 기동 거부.
ENGINE_CONFIG_BY_ALGORITHM = {"gme-motion-v1": GMEConfig.v26, "gme-motion-v2": GMEConfig.v2_link}


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
        "tracker_provenance": {"implementation": "opencv-sparse-lk-v0" if job.algorithm_version == "gme-motion-v1"
                               else "anchor-link-v1"},  # v2: 1초 공백 유지·공백 채우기·IoU 흔들림 가드
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
            "bbox_coordinate_contract": detector.bbox_coordinate_contract,
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
    """DB/R2 접근 전에 승인된 detector와 기존 GME 활동시간 계약을 검증한다."""

    expected = {
        "GME_DETECTOR_BACKEND": "yolo26n",
        "GME_CHECKPOINT_SHA256": V26_CHECKPOINT_SHA256,
        "GME_DETECTOR_FREEZE_SHA256": V26_DETECTOR_FREEZE_SHA256,
        "GME_DETECTOR_IDENTITY": V26_DETECTOR_IDENTITY,
        "GME_ALGORITHM_VERSION": "gme-motion-v1",
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
    if config.GME_MODEL_VERSION == "v2.6.1-warm-start-s27":
        threshold = config.GME_SCORE_THRESHOLD if config.GME_SCORE_THRESHOLD in V261_IDENTITY_BY_THRESHOLD else 0.30
        expected.update({
            "GME_MODEL_VERSION": "v2.6.1-warm-start-s27",
            "GME_CHECKPOINT_SHA256": "313e933b623561027af9368244c16e8960c74d9b66ec1b919c8d3b58c0ff1c96",
            "GME_DETECTOR_FREEZE_SHA256": "a0c9d8305ee263a4302f7a0c0fc04a04e75b47b00815d91c088f56df42438bef",
            "GME_DETECTOR_IDENTITY": V261_IDENTITY_BY_THRESHOLD[threshold],
            "GME_SCORE_THRESHOLD": threshold,
        })
    elif config.GME_MODEL_VERSION == BETA_V2_MODEL_VERSION:
        if config.GME_ALGORITHM_VERSION != "gme-motion-v2":
            raise ValueError("approved YOLO contract mismatch: GME_ALGORITHM_VERSION")
        expected.update({
            "GME_MODEL_VERSION": BETA_V2_MODEL_VERSION,
            "GME_CHECKPOINT_SHA256": BETA_V2_CHECKPOINT_SHA256,
            "GME_DETECTOR_FREEZE_SHA256": BETA_V2_DETECTOR_FREEZE_SHA256,
            "GME_DETECTOR_IDENTITY": BETA_V2_DETECTOR_IDENTITY,
            "GME_SCORE_THRESHOLD": 0.80,
        })
    elif config.GME_MODEL_VERSION == BETA_V3_MODEL_VERSION:
        if config.GME_ALGORITHM_VERSION != "gme-motion-v2":
            raise ValueError("approved YOLO contract mismatch: GME_ALGORITHM_VERSION")
        expected.update({
            "GME_MODEL_VERSION": BETA_V3_MODEL_VERSION,
            "GME_CHECKPOINT_SHA256": BETA_V3_CHECKPOINT_SHA256,
            "GME_DETECTOR_FREEZE_SHA256": BETA_V3_DETECTOR_FREEZE_SHA256,
            "GME_DETECTOR_IDENTITY": BETA_V3_DETECTOR_IDENTITY,
            "GME_SCORE_THRESHOLD": 0.70,
        })
    elif config.GME_MODEL_VERSION == BETA_V4_MODEL_VERSION:
        if config.GME_ALGORITHM_VERSION != "gme-motion-v2":
            raise ValueError("approved YOLO contract mismatch: GME_ALGORITHM_VERSION")
        expected.update({
            "GME_MODEL_VERSION": BETA_V4_MODEL_VERSION,
            "GME_CHECKPOINT_SHA256": BETA_V4_CHECKPOINT_SHA256,
            "GME_DETECTOR_FREEZE_SHA256": BETA_V4_DETECTOR_FREEZE_SHA256,
            "GME_DETECTOR_IDENTITY": BETA_V4_DETECTOR_IDENTITY,
            "GME_SCORE_THRESHOLD": 0.70,
        })
    if config.GME_ALGORITHM_VERSION not in ENGINE_CONFIG_BY_ALGORITHM:
        raise ValueError("approved YOLO contract mismatch: GME_ALGORITHM_VERSION")
    expected["GME_ALGORITHM_VERSION"] = config.GME_ALGORITHM_VERSION
    for name, value in expected.items():
        if getattr(config, name) != value:
            raise ValueError(f"approved YOLO contract mismatch: {name}")
    engine_config = ENGINE_CONFIG_BY_ALGORITHM[config.GME_ALGORITHM_VERSION]()
    if (
        algorithm_version_for(engine_config) != config.GME_ALGORITHM_VERSION
        or engine_config.analysis_fps != config.GME_ANALYSIS_FPS
        or engine_config.anchor_interval_sec != config.GME_ANCHOR_INTERVAL_SEC
        or engine_config.detection_window_frames != config.GME_TEMPORAL_WINDOW_FRAMES
        or engine_config.detection_min_positive_frames != config.GME_TEMPORAL_MIN_POSITIVE_FRAMES
        or not engine_config.detector_every_analysis_frame
    ):
        raise ValueError("local Gate v2.6 engine contract mismatch")
    return engine_config


def _job_contract_ok(job, detector_provenance: dict) -> bool:
    return (
        job.engine_schema_version == ENGINE_SCHEMA_VERSION
        and job.algorithm_version == config.GME_ALGORITHM_VERSION
        and job.detector_identity == detector_provenance.get("detector_identity")
    )


def process_jobs(
    sb, jobs, clip_keys, *, worker_host: str, now: datetime, temp_root: Path | None,
    download_fn, analyze_fn, serialize_fn, upload_fn, insert_fn, complete_fn, fail_fn,
    producer: Producer,
    detector_provenance: dict,
) -> dict:
    stats = {"jobs": len(jobs), "succeeded": 0, "failed": 0, "terminal": 0, "stale": 0}
    for job in jobs:
        try:
            if not _job_contract_ok(job, detector_provenance):
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
                if (
                    analysis.artifact_identity.engine_schema_version != job.engine_schema_version
                    or analysis.artifact_identity.algorithm_version != job.algorithm_version
                    or analysis.artifact_identity.detector_identity != job.detector_identity
                ):
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
        queue_stats = operational_stats(
            sb,
            now=now,
            detector_identity=config.GME_DETECTOR_IDENTITY,
            algorithm_version=config.GME_ALGORITHM_VERSION,
            engine_schema_version=ENGINE_SCHEMA_VERSION,
        )
        include_historical = allow_historical_claim(queue_stats, max_live_lag_sec=config.GME_MAX_LIVE_LAG_SEC)
        allowed_sources = tuple(
            source for source in config.GME_ALLOWED_SOURCES
            if source != "historical" or include_historical
        )
        if not allowed_sources:
            print("[gme] historical claim paused by live lag — skip")
            return 0
        jobs = claim_jobs(sb, limit=config.GME_BATCH_LIMIT, worker_host=host, now=now,
                          allowed_sources=allowed_sources,
                          detector_identity=config.GME_DETECTOR_IDENTITY,
                          algorithm_version=config.GME_ALGORITHM_VERSION,
                          engine_schema_version=ENGINE_SCHEMA_VERSION)
        if not jobs:
            print("[gme] no jobs — skip")
            return 0
        clip_keys = load_clip_r2_keys(sb, [job.clip_id for job in jobs])
        detector, detector_provenance = _build_runtime_detector()
        if detector_provenance.get("detector_identity") != config.GME_DETECTOR_IDENTITY:
            print("[gme] detector execution identity mismatch", file=sys.stderr)
            return 2
        producer = Producer(host, now.strftime("%Y%m%dT%H%M%S"), f"gme-worker/{config.GME_ALGORITHM_VERSION}")
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
