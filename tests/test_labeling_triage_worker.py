from pathlib import Path

import pytest

from gecko_vision_gate.activity_policy import ActivityAssessment, ActivityPolicy
from gecko_vision_gate.motion_evidence import MotionMetrics
from gecko_vision_gate.provenance import GateProvenance
from gecko_vision_gate.schema import PrelabelResult

from reporter.gate_runner import GateAssessment
from reporter.labeling_triage_models import LabelingTriageClip
from reporter.labeling_triage_store import StoreResult
from reporter.labeling_triage_worker import process_triage_batch, run


def _clip(n=1):
    return LabelingTriageClip(
        id=f"00000000-0000-0000-0000-{n:012d}",
        camera_id="camera",
        started_at="2026-07-15T00:00:00+00:00",
        duration_sec=30.0,
        r2_key=f"clip-{n}.mp4",
    )


def _gate(decision="active"):
    return GateAssessment(
        PrelabelResult(decision != "exclude_absent", 0.8, 12, "rfdetr", "gecko-v2"),
        MotionMetrics(8, 0.667, 0.03, 0.1, 0.8, 0.7, 0.2, False),
        ActivityAssessment(decision, "reason", {"policy_version": "activity-v1"}),
        GateProvenance("rfdetr", "gecko-v2", "a" * 64, 0.1, "sampler", "schema", 12),
    )


def _download(_key, dest: Path):
    dest.write_bytes(b"mp4")
    return dest


@pytest.mark.parametrize("decision", ["active", "exclude_absent", "exclude_static"])
def test_write_mode_stores_mapped_suggestions(decision):
    stored = []
    stats = process_triage_batch(
        object(), [_clip()], object(), ActivityPolicy("activity-v1", 0.1), "checkpoint",
        "labeling-triage-v1", write_enabled=True, download_fn=_download,
        assess_fn=lambda *_args, **_kwargs: _gate(decision),
        store_fn=lambda _sb, suggestion: stored.append(suggestion) or StoreResult("stored"),
    )
    assert len(stored) == 1
    assert stats["assessed"] == 1
    assert stats["stored_label" if decision == "active" else f"stored_quarantine_{decision.removeprefix('exclude_')}"] == 1
    assert stats["temp_files_remaining"] == 0


def test_preview_assesses_but_never_calls_store():
    stats = process_triage_batch(
        object(), [_clip()], object(), ActivityPolicy("activity-v1", 0.1), "checkpoint",
        "labeling-triage-v1", write_enabled=False, download_fn=_download,
        assess_fn=lambda *_args, **_kwargs: _gate("active"),
        store_fn=lambda *_args: pytest.fail("preview called store"),
    )
    assert stats["assessed"] == 1
    assert stats["stored_label"] == 0


def test_unknown_and_clip_failures_are_fail_open_and_continue():
    clips = [_clip(1), _clip(2), _clip(3)]

    def download(key, dest):
        if key == "clip-1.mp4":
            raise OSError("secret signed url")
        return _download(key, dest)

    def assess(path, *_args, **_kwargs):
        return _gate("unknown" if path.endswith("2.mp4") else "active")

    stats = process_triage_batch(
        object(), clips, object(), ActivityPolicy("activity-v1", 0.1), "checkpoint",
        "labeling-triage-v1", write_enabled=False, download_fn=download,
        assess_fn=assess, store_fn=lambda *_args: pytest.fail("preview called store"),
    )
    assert stats["failed_download"] == 1
    assert stats["unknown"] == 1
    assert stats["assessed"] == 2
    assert stats["temp_files_remaining"] == 0


def test_store_failure_aborts_instead_of_fake_success():
    with pytest.raises(RuntimeError, match="db down"):
        process_triage_batch(
            object(), [_clip()], object(), ActivityPolicy("activity-v1", 0.1), "checkpoint",
            "labeling-triage-v1", write_enabled=True, download_fn=_download,
            assess_fn=lambda *_args, **_kwargs: _gate("active"),
            store_fn=lambda *_args: (_ for _ in ()).throw(RuntimeError("db down")),
        )


def test_partial_download_and_gate_failure_leave_no_temp_files():
    def partial_download(_key, dest):
        dest.write_bytes(b"partial")
        raise OSError("failed")

    first = process_triage_batch(
        object(), [_clip()], object(), ActivityPolicy("activity-v1", 0.1), "checkpoint",
        "labeling-triage-v1", write_enabled=True, download_fn=partial_download,
        assess_fn=lambda *_args, **_kwargs: _gate(), store_fn=lambda *_args: StoreResult("stored"),
    )
    second = process_triage_batch(
        object(), [_clip()], object(), ActivityPolicy("activity-v1", 0.1), "checkpoint",
        "labeling-triage-v1", write_enabled=True, download_fn=_download,
        assess_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("decode")),
        store_fn=lambda *_args: StoreResult("stored"),
    )
    assert first["temp_files_remaining"] == 0
    assert second["temp_files_remaining"] == 0


def test_disabled_run_does_not_touch_db_or_detector():
    calls = []
    assert run(
        enabled=False,
        create_client_fn=lambda *_args: calls.append("db"),
        load_detector_fn=lambda *_args: calls.append("detector"),
    ) == 0
    assert calls == []


def test_run_loads_detector_once_for_candidates():
    calls = []
    assert run(
        sb=object(), enabled=True, write_enabled=False,
        list_candidates_fn=lambda *_args, **_kwargs: [_clip(1), _clip(2)],
        load_detector_fn=lambda *_args: calls.append("detector") or object(),
        download_fn=_download,
        assess_fn=lambda *_args, **_kwargs: _gate("active"),
        acquire_lock_fn=lambda: object(), release_lock_fn=lambda _lock: None,
    ) == 0
    assert calls == ["detector"]
