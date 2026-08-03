from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from gecko_vision_gate.gme_contracts import ArtifactIdentity, GMEAnalysis, StateInterval, TrackingQuality

from reporter import gme_worker as worker
from reporter.gme_artifacts import UploadedArtifacts
from reporter.gme_store import GMEJob


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def _job():
    return GMEJob("job-1", "clip-1", "live", 100, "gme-shadow-v1", "gme-motion-v0", "a" * 64, "processing", 1)


def _analysis():
    base = GMEAnalysis.minimal(
        duration_sec=1.0,
        intervals=(StateInterval(0.0, 1.0, "static", ("g0001",)),),
        tracking_quality=TrackingQuality.empty(),
        artifact_identity=ArtifactIdentity("gme-shadow-v1", "gme-motion-v0", "a" * 64),
    )
    return replace(base, decoded_frame_count=30, analyzed_frame_count=30, source_fps=30.0)


def test_disabled_worker_has_zero_side_effects(monkeypatch):
    monkeypatch.setattr(worker.config, "GME_ENABLED", False)
    called = []
    assert worker.run(sb_factory=lambda: called.append("db")) == 0
    assert called == []


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
    )
    assert stats["succeeded"] == 1 and stats["failed"] == 1
    assert failed == ["r2_download_failed"]
    assert list(tmp_path.iterdir()) == []
