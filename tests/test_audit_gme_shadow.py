from scripts.audit_gme_shadow import evaluate_smoke


V25_IDENTITY = "d4654168af21d26697ab1bd9a5dc4a05bd92baf5c9328800915cc347803d05b6"


def _job(index):
    return {
        "id": f"j{index}", "clip_id": f"c{index}", "status": "succeeded",
        "result_run_id": f"r{index}", "detector_identity": V25_IDENTITY,
    }


def _run(index):
    return {
        "id": f"r{index}", "job_id": f"j{index}",
        "permanent_artifact_key": f"terra-derived/gme/v1/permanent/c{index}/" + "a" * 64 + ".json.gz",
        "permanent_artifact_sha256": "b" * 64,
        "debug_artifact_key": f"terra-derived/gme/v1/debug-14d/c{index}/" + "a" * 64 + ".json.gz",
        "debug_artifact_sha256": "c" * 64,
    }


def test_smoke_audit_requires_ten_unique_successes_and_both_artifacts():
    report = evaluate_smoke(
        [_job(i) for i in range(10)], [_run(i) for i in range(10)],
        detector_identity=V25_IDENTITY, head_fn=lambda *_: True,
    )
    assert report == {"complete": 10, "unique_clips": 10, "artifacts": 20, "ok": True}


def test_smoke_audit_fails_closed_on_missing_artifact_or_duplicate_clip():
    jobs = [_job(i) for i in range(10)]
    jobs[-1]["clip_id"] = jobs[0]["clip_id"]
    report = evaluate_smoke(
        jobs, [_run(i) for i in range(10)], detector_identity=V25_IDENTITY,
        head_fn=lambda key, _sha: "debug-14d" not in key,
    )
    assert report["ok"] is False


def test_smoke_audit_rejects_other_detector_identity():
    jobs = [_job(i) for i in range(10)]
    jobs[-1]["detector_identity"] = "a" * 64
    report = evaluate_smoke(
        jobs, [_run(i) for i in range(10)], detector_identity=V25_IDENTITY, head_fn=lambda *_: True,
    )
    assert report["ok"] is False
