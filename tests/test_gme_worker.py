from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from gecko_vision_gate.gme_contracts import ArtifactIdentity, GMEAnalysis, StateInterval, TrackingQuality

from reporter import gme_worker as worker
from reporter.gme_artifacts import UploadedArtifacts
from reporter.gme_store import GMEJob


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)
V26_SHA = "a00e5a7a1e1f9197accb036339a38a7c821f03c8ab79611ebce89e5cde59b513"
V26_FREEZE_SHA = "8f8e02beb452ec2ddfdce344dff507294f56136c69224990c50552d22bb343a0"
V26_IDENTITY = "deccfc8315d3c00edb5bf59db3c573dca568e9d6d7a5da8d7dc93d2082bdb899"
BETA_V2_SHA = "91a275ad1a047e7a0f70a8a9c1212da7d2b21532bb568a33b3401be9e338751d"
BETA_V2_FREEZE_SHA = "74d2dc854198b8f871005d591dc18bd5273d7de857ebcea11b4d32f60343c75f"
BETA_V2_IDENTITY = "8968c0546e4a86486eb56fb1ab89939da9aedabe3306131ffe8f052669ee0964"
ALGORITHM_VERSION = "gme-motion-v1"
V26_PROVENANCE = {
    "model_name": "yolo26n",
    "model_version": "v2.6-warm-start-s28",
    "checkpoint_sha256": V26_SHA,
    "detector_freeze_sha256": V26_FREEZE_SHA,
    "detector_identity": V26_IDENTITY,
    "bbox_coordinate_contract": "xywh-top-left-v1",
    "raw_confidence": 0.001,
    "threshold": 0.15,
    "image_size": 960,
    "model_nms_iou": 0.70,
    "post_nms_iou": 0.55,
    "max_detections": 50,
    "analysis_fps": 10.0,
    "temporal_window_frames": 5,
    "temporal_min_positive_frames": 3,
}


def _job():
    return GMEJob("job-1", "clip-1", "live", 100, "gme-shadow-v1", ALGORITHM_VERSION, V26_IDENTITY, "processing", 1)


def _analysis():
    base = GMEAnalysis.minimal(
        duration_sec=1.0,
        intervals=(StateInterval(0.0, 1.0, "static", ("g0001",)),),
        tracking_quality=TrackingQuality.empty(),
        artifact_identity=ArtifactIdentity("gme-shadow-v1", ALGORITHM_VERSION, V26_IDENTITY),
    )
    return replace(base, decoded_frame_count=30, analyzed_frame_count=30, source_fps=30.0)


def test_disabled_worker_has_zero_side_effects(monkeypatch):
    monkeypatch.setattr(worker.config, "GME_ENABLED", False)
    called = []
    assert worker.run(sb_factory=lambda: called.append("db")) == 0
    assert called == []


def test_v261_runtime_contract_is_accepted_and_mixed_identity_rejected(monkeypatch):
    import pytest
    values = {
        "GME_DETECTOR_BACKEND": "yolo26n", "GME_ALGORITHM_VERSION": "gme-motion-v1",
        "GME_MODEL_VERSION": "v2.6.1-warm-start-s27",
        "GME_CHECKPOINT_SHA256": "313e933b623561027af9368244c16e8960c74d9b66ec1b919c8d3b58c0ff1c96",
        "GME_DETECTOR_FREEZE_SHA256": "a0c9d8305ee263a4302f7a0c0fc04a04e75b47b00815d91c088f56df42438bef",
        "GME_DETECTOR_IDENTITY": "44dd382cba74355c98eff3c202acf1b1195d92870c50ac86f4fafa4c75aed92e",
        "GME_RAW_CONFIDENCE": .001, "GME_SCORE_THRESHOLD": .30,
        "GME_IMAGE_SIZE": 960, "GME_NMS_IOU": .70, "GME_POST_NMS_IOU": .55,
        "GME_MAX_DETECTIONS": 50, "GME_ANALYSIS_FPS": 10.,
        "GME_ANCHOR_INTERVAL_SEC": .1, "GME_TEMPORAL_WINDOW_FRAMES": 5,
        "GME_TEMPORAL_MIN_POSITIVE_FRAMES": 3, "GME_DEVICE": "mps",
    }
    for key, value in values.items():
        monkeypatch.setattr(worker.config, key, value)
    assert worker._validated_v26_engine_config().analysis_fps == 10.0
    monkeypatch.setattr(worker.config, "GME_DETECTOR_IDENTITY", V26_IDENTITY)
    with pytest.raises(ValueError):
        worker._validated_v26_engine_config()


def test_run_passes_configured_identity_to_claim_rpc(monkeypatch):
    captured = {}
    monkeypatch.setattr(worker.config, "GME_ENABLED", True)
    monkeypatch.setattr(worker.config, "GME_EXPECTED_HOST", "expected")
    monkeypatch.setattr(worker.config, "GME_DETECTOR_IDENTITY", V26_IDENTITY)
    monkeypatch.setattr(worker.config, "GME_ALGORITHM_VERSION", ALGORITHM_VERSION)
    monkeypatch.setattr(worker.config, "GME_ALLOWED_SOURCES", ("smoke", "live"))
    monkeypatch.setattr(worker, "_validated_v26_engine_config", lambda: object())
    def fake_operational_stats(_sb, **kwargs):
        captured["stats_contract"] = kwargs
        return {}

    monkeypatch.setattr(worker, "operational_stats", fake_operational_stats)
    monkeypatch.setattr(worker, "allow_historical_claim", lambda *_args, **_kwargs: False)

    def fake_claim(_sb, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(worker, "claim_jobs", fake_claim)
    rc = worker.run(
        sb_factory=lambda: object(), hostname_fn=lambda: "expected", now=NOW,
        acquire_lock_fn=lambda: object(), release_lock_fn=lambda _lock: None,
    )

    assert rc == 0
    assert captured["detector_identity"] == V26_IDENTITY
    assert captured["algorithm_version"] == ALGORITHM_VERSION
    assert captured["allowed_sources"] == ("smoke", "live")
    assert captured["stats_contract"] == {
        "now": NOW,
        "detector_identity": V26_IDENTITY,
        "algorithm_version": ALGORITHM_VERSION,
        "engine_schema_version": "gme-shadow-v1",
    }


def test_historical_only_worker_pauses_without_claim_when_live_lag_is_high(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.config, "GME_ENABLED", True)
    monkeypatch.setattr(worker.config, "GME_EXPECTED_HOST", "expected")
    monkeypatch.setattr(worker.config, "GME_DETECTOR_IDENTITY", V26_IDENTITY)
    monkeypatch.setattr(worker.config, "GME_ALGORITHM_VERSION", ALGORITHM_VERSION)
    monkeypatch.setattr(worker.config, "GME_ALLOWED_SOURCES", ("historical",))
    monkeypatch.setattr(worker, "_validated_v26_engine_config", lambda: object())
    monkeypatch.setattr(worker, "operational_stats", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(worker, "allow_historical_claim", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(worker, "claim_jobs", lambda *_args, **_kwargs: calls.append("claim"))

    rc = worker.run(
        sb_factory=lambda: object(), hostname_fn=lambda: "expected", now=NOW,
        acquire_lock_fn=lambda: object(), release_lock_fn=lambda _lock: None,
    )

    assert rc == 0
    assert calls == []


def test_process_jobs_downloads_analyzes_uploads_inserts_and_completes_once(tmp_path):
    calls = []
    artifacts = UploadedArtifacts("p", "b" * 64, 10, "d", "c" * 64, 20)
    stats = worker.process_jobs(
        object(), [_job()], {"clip-1": "terra-clips/clips/c.mp4"}, worker_host="host", now=NOW,
        temp_root=tmp_path,
        download_fn=lambda key, dest: (calls.append(("download", key)), dest.write_bytes(b"video")),
        analyze_fn=lambda path: (calls.append(("analyze", path.name)), _analysis())[1],
        serialize_fn=lambda analysis: type("S", (), {"permanent_gzip": b"p", "debug_gzip": b"d", "permanent_sha256": "e" * 64})(),
        upload_fn=lambda **kwargs: (calls.append(("upload", kwargs["clip_id"])), artifacts)[1],
        insert_fn=lambda sb, payload: (calls.append(("insert", payload["status"])), {"id": "run-1"})[1],
        complete_fn=lambda sb, **kwargs: calls.append(("complete", kwargs["run_id"])),
        fail_fn=lambda *a, **k: calls.append(("fail", k.get("failure_code"))),
        producer=worker.Producer("host", "run-1", "code-ref"),
        detector_provenance=V26_PROVENANCE,
    )
    assert [c[0] for c in calls] == ["download", "analyze", "upload", "insert", "complete"]
    assert stats == {"jobs": 1, "succeeded": 1, "failed": 0, "terminal": 0, "stale": 0}
    assert list(tmp_path.iterdir()) == []


def test_one_job_failure_does_not_block_next_and_temp_is_zero(tmp_path):
    jobs = [_job(), replace(_job(), id="job-2", clip_id="clip-2")]
    failed = []

    def download(key, dest):
        if "one" in key:
            raise RuntimeError("network")
        dest.write_bytes(b"video")

    stats = worker.process_jobs(
        object(), jobs, {"clip-1": "one", "clip-2": "two"}, worker_host="host", now=NOW,
        temp_root=tmp_path, download_fn=download, analyze_fn=lambda _: _analysis(),
        serialize_fn=lambda _: type("S", (), {"permanent_gzip": b"p", "debug_gzip": b"d", "permanent_sha256": "e" * 64})(),
        upload_fn=lambda **_: UploadedArtifacts("p", "b" * 64, 1, "d", "c" * 64, 1),
        insert_fn=lambda *_a, **_k: {"id": "run"}, complete_fn=lambda *_a, **_k: None,
        fail_fn=lambda *_a, **kwargs: failed.append(kwargs["failure_code"]),
        producer=worker.Producer("host", "run", "code"),
        detector_provenance=V26_PROVENANCE,
    )
    assert stats["succeeded"] == 1 and stats["failed"] == 1
    assert failed == ["r2_download_failed"]
    assert list(tmp_path.iterdir()) == []


def test_detector_identity_mismatch_is_terminal_before_media_download(tmp_path):
    calls = []
    mismatched = replace(_job(), detector_identity="b" * 64)

    stats = worker.process_jobs(
        object(), [mismatched], {"clip-1": "terra-clips/clips/c.mp4"},
        worker_host="host", now=NOW, temp_root=tmp_path,
        download_fn=lambda *_args: calls.append("download"),
        analyze_fn=lambda _path: _analysis(), serialize_fn=lambda _analysis: object(),
        upload_fn=lambda **_kwargs: object(), insert_fn=lambda *_args: {"id": "run"},
        complete_fn=lambda *_args, **_kwargs: None,
        fail_fn=lambda *_args, **kwargs: calls.append(kwargs["failure_code"]),
        producer=worker.Producer("host", "run", "code"),
        detector_provenance=V26_PROVENANCE,
    )

    assert calls == ["invalid_metadata"]
    assert stats == {"jobs": 1, "succeeded": 0, "failed": 1, "terminal": 1, "stale": 0}
    assert list(tmp_path.iterdir()) == []


def test_algorithm_mismatch_is_terminal_before_media_download(tmp_path):
    calls = []
    mismatched = replace(_job(), algorithm_version="gme-motion-v0")

    stats = worker.process_jobs(
        object(), [mismatched], {"clip-1": "terra-clips/clips/c.mp4"},
        worker_host="host", now=NOW, temp_root=tmp_path,
        download_fn=lambda *_args: calls.append("download"),
        analyze_fn=lambda _path: _analysis(), serialize_fn=lambda _analysis: object(),
        upload_fn=lambda **_kwargs: object(), insert_fn=lambda *_args: {"id": "run"},
        complete_fn=lambda *_args, **_kwargs: None,
        fail_fn=lambda *_args, **kwargs: calls.append(kwargs["failure_code"]),
        producer=worker.Producer("host", "run", "code"),
        detector_provenance=V26_PROVENANCE,
    )

    assert calls == ["invalid_metadata"]
    assert stats["terminal"] == 1
    assert list(tmp_path.iterdir()) == []


def test_run_payload_persists_exact_detector_inference_contract():
    payload = worker._run_payload(
        _job(), _analysis(), UploadedArtifacts("p", "b" * 64, 10, "d", "c" * 64, 20),
        worker.Producer("host", "run-1", "code-ref"),
        detector_provenance=V26_PROVENANCE,
    )

    assert payload["detector_provenance"] == V26_PROVENANCE


def test_runtime_detector_uses_exact_v26_yolo_contract(monkeypatch):
    captured = {}
    fake_detector = type(
        "Detector",
        (),
        {
            "model_name": "yolo26n", "model_version": "v2.6-warm-start-s28",
            "checkpoint_sha256": V26_SHA, "raw_confidence": 0.001,
            "schema_version": "gate-evidence-v1",
            "threshold": 0.15, "image_size": 960, "nms_iou": 0.70,
            "post_nms_iou": 0.55, "max_detections": 50,
            "execution_identity": V26_IDENTITY,
            "bbox_coordinate_contract": "xywh-top-left-v1",
        },
    )()
    monkeypatch.setattr(worker.config, "GME_DETECTOR_BACKEND", "yolo26n")
    monkeypatch.setattr(worker.config, "GME_CHECKPOINT_PATH", "/private/best.pt")
    monkeypatch.setattr(worker.config, "GME_CHECKPOINT_SHA256", V26_SHA)
    monkeypatch.setattr(worker.config, "GME_DETECTOR_FREEZE_SHA256", V26_FREEZE_SHA)
    monkeypatch.setattr(worker.config, "GME_DETECTOR_IDENTITY", V26_IDENTITY)
    monkeypatch.setattr(worker.config, "GME_ALGORITHM_VERSION", ALGORITHM_VERSION)
    monkeypatch.setattr(worker.config, "GME_MODEL_VERSION", "v2.6-warm-start-s28")
    monkeypatch.setattr(worker.config, "GME_RAW_CONFIDENCE", 0.001)
    monkeypatch.setattr(worker.config, "GME_SCORE_THRESHOLD", 0.15)
    monkeypatch.setattr(worker.config, "GME_IMAGE_SIZE", 960)
    monkeypatch.setattr(worker.config, "GME_NMS_IOU", 0.70)
    monkeypatch.setattr(worker.config, "GME_POST_NMS_IOU", 0.55)
    monkeypatch.setattr(worker.config, "GME_MAX_DETECTIONS", 50)
    monkeypatch.setattr(worker.config, "GME_ANALYSIS_FPS", 10.0)
    monkeypatch.setattr(worker.config, "GME_TEMPORAL_WINDOW_FRAMES", 5)
    monkeypatch.setattr(worker.config, "GME_TEMPORAL_MIN_POSITIVE_FRAMES", 3)
    monkeypatch.setattr(worker.config, "GME_DEVICE", "mps")
    monkeypatch.setattr(
        worker,
        "build_yolo_detector",
        lambda **kwargs: (captured.update(kwargs), fake_detector)[1],
    )

    detector, provenance = worker._build_runtime_detector()

    assert detector is fake_detector
    assert captured == {
        "checkpoint": "/private/best.pt", "expected_sha256": V26_SHA,
        "model_version": "v2.6-warm-start-s28", "raw_confidence": 0.001,
        "score_threshold": 0.15, "image_size": 960, "nms_iou": 0.70,
        "post_nms_iou": 0.55, "max_detections": 50, "analysis_fps": 10.0,
        "temporal_window_frames": 5, "temporal_min_positive_frames": 3,
        "device": "mps",
    }
    assert provenance == V26_PROVENANCE


def test_v26_contract_mismatch_stops_before_db(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.config, "GME_ENABLED", True)
    monkeypatch.setattr(worker.config, "GME_EXPECTED_HOST", "expected")
    monkeypatch.setattr(worker.config, "GME_DETECTOR_BACKEND", "yolo26n")
    monkeypatch.setattr(worker.config, "GME_SCORE_THRESHOLD", 0.20)

    rc = worker.run(
        sb_factory=lambda: calls.append("db"), hostname_fn=lambda: "expected",
        acquire_lock_fn=lambda: object(), release_lock_fn=lambda _lock: calls.append("unlock"),
    )

    assert rc == 2
    assert calls == ["unlock"]


def test_unsupported_detector_backend_stops_before_db(monkeypatch):
    calls = []
    monkeypatch.setattr(worker.config, "GME_ENABLED", True)
    monkeypatch.setattr(worker.config, "GME_EXPECTED_HOST", "expected")
    monkeypatch.setattr(worker.config, "GME_DETECTOR_BACKEND", "unknown")

    rc = worker.run(
        sb_factory=lambda: calls.append("db"), hostname_fn=lambda: "expected",
        acquire_lock_fn=lambda: object(), release_lock_fn=lambda _lock: calls.append("unlock"),
    )

    assert rc == 2
    assert calls == ["unlock"]


# ── gme-motion-v2 (2026-09-23): 추적 연결 완화 + v2.6.1 threshold 0.15 ──

V261_015_IDENTITY = "81688d0a5ae61299e569573bbc7429bbe362ef9f086cd1cf3e699f9c18c5de2c"


def _v261_values(**over):
    values = {
        "GME_DETECTOR_BACKEND": "yolo26n", "GME_ALGORITHM_VERSION": "gme-motion-v1",
        "GME_MODEL_VERSION": "v2.6.1-warm-start-s27",
        "GME_CHECKPOINT_SHA256": "313e933b623561027af9368244c16e8960c74d9b66ec1b919c8d3b58c0ff1c96",
        "GME_DETECTOR_FREEZE_SHA256": "a0c9d8305ee263a4302f7a0c0fc04a04e75b47b00815d91c088f56df42438bef",
        "GME_DETECTOR_IDENTITY": "44dd382cba74355c98eff3c202acf1b1195d92870c50ac86f4fafa4c75aed92e",
        "GME_RAW_CONFIDENCE": .001, "GME_SCORE_THRESHOLD": .30,
        "GME_IMAGE_SIZE": 960, "GME_NMS_IOU": .70, "GME_POST_NMS_IOU": .55,
        "GME_MAX_DETECTIONS": 50, "GME_ANALYSIS_FPS": 10.,
        "GME_ANCHOR_INTERVAL_SEC": .1, "GME_TEMPORAL_WINDOW_FRAMES": 5,
        "GME_TEMPORAL_MIN_POSITIVE_FRAMES": 3, "GME_DEVICE": "mps",
    }
    values.update(over)
    return values


def test_v2_contract_uses_link_engine_and_015_identity(monkeypatch):
    for key, value in _v261_values(GME_ALGORITHM_VERSION="gme-motion-v2", GME_SCORE_THRESHOLD=.15,
                                   GME_DETECTOR_IDENTITY=V261_015_IDENTITY).items():
        monkeypatch.setattr(worker.config, key, value)
    cfg = worker._validated_v26_engine_config()
    assert cfg.track_max_missed_sec == 1.0 and cfg.fill_track_gaps is True


def test_beta_v2_contract_accepts_only_frozen_model_tuple(monkeypatch):
    values = _v261_values(
        GME_ALGORITHM_VERSION="gme-motion-v2",
        GME_MODEL_VERSION="v2.6.1-beta-v2-s27",
        GME_CHECKPOINT_SHA256=BETA_V2_SHA,
        GME_DETECTOR_FREEZE_SHA256=BETA_V2_FREEZE_SHA,
        GME_DETECTOR_IDENTITY=BETA_V2_IDENTITY,
        GME_SCORE_THRESHOLD=.80,
    )
    for key, value in values.items():
        monkeypatch.setattr(worker.config, key, value)
    assert worker._validated_v26_engine_config().analysis_fps == 10.0
    monkeypatch.setattr(worker.config, "GME_SCORE_THRESHOLD", .15)
    import pytest
    with pytest.raises(ValueError, match="GME_SCORE_THRESHOLD"):
        worker._validated_v26_engine_config()
    monkeypatch.setattr(worker.config, "GME_SCORE_THRESHOLD", .80)
    monkeypatch.setattr(worker.config, "GME_ALGORITHM_VERSION", "gme-motion-v1")
    with pytest.raises(ValueError, match="GME_ALGORITHM_VERSION"):
        worker._validated_v26_engine_config()


def test_config_accepts_canonical_source_allowlists_only():
    assert worker.config.parse_gme_allowed_sources("smoke,live") == ("smoke", "live")
    assert worker.config.parse_gme_allowed_sources("historical") == ("historical",)
    import pytest
    for raw in ("", "live,smoke", "live,live", "live, historical", "other"):
        with pytest.raises(ValueError, match="GME_ALLOWED_SOURCES"):
            worker.config.parse_gme_allowed_sources(raw)


def test_v2_algorithm_with_030_threshold_is_also_allowed(monkeypatch):
    for key, value in _v261_values(GME_ALGORITHM_VERSION="gme-motion-v2").items():
        monkeypatch.setattr(worker.config, key, value)
    assert worker._validated_v26_engine_config().track_max_missed_sec == 1.0


def test_015_threshold_with_old_identity_is_rejected(monkeypatch):
    import pytest
    for key, value in _v261_values(GME_ALGORITHM_VERSION="gme-motion-v2", GME_SCORE_THRESHOLD=.15).items():
        monkeypatch.setattr(worker.config, key, value)
    with pytest.raises(ValueError):
        worker._validated_v26_engine_config()


def test_unknown_algorithm_version_rejected(monkeypatch):
    import pytest
    for key, value in _v261_values(GME_ALGORITHM_VERSION="gme-motion-v9").items():
        monkeypatch.setattr(worker.config, key, value)
    with pytest.raises(ValueError):
        worker._validated_v26_engine_config()


def test_process_jobs_accepts_configured_v2_jobs(monkeypatch):
    monkeypatch.setattr(worker.config, "GME_ALGORITHM_VERSION", "gme-motion-v2")
    job = GMEJob("job-2", "clip-2", "live", 100, "gme-shadow-v1", "gme-motion-v2", V26_IDENTITY, "processing", 1)
    assert worker._job_contract_ok(job, {"detector_identity": V26_IDENTITY})
    v1_job = GMEJob("job-3", "clip-3", "live", 100, "gme-shadow-v1", "gme-motion-v1", V26_IDENTITY, "processing", 1)
    assert not worker._job_contract_ok(v1_job, {"detector_identity": V26_IDENTITY})
