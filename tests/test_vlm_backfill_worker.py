from datetime import datetime, timedelta, timezone

import pytest

from reporter.vlm_backfill_gate import GateEnrichment
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION, bucket_plans, source_nights
from reporter.vlm_backfill_worker import (
    InsufficientCandidates,
    choose_target_camera,
    next_source_date,
    prepare_wave,
    run,
)
from reporter.vlm_models import CandidateClip, SelectedCandidate
from tests._fakes import FakeSB


def _motion_clip(cid: str, camera: str, started_at: datetime) -> dict:
    return {
        "id": cid,
        "camera_id": camera,
        "started_at": started_at.isoformat(),
        "duration_sec": 30,
        "r2_key": f"clips/{cid}.mp4",
        "motion_score": 1,
        "width": 1280,
        "height": 720,
    }


def _candidate(cid: str, started_at: datetime) -> CandidateClip:
    return CandidateClip(cid, "camera-a", started_at, 30, f"clips/{cid}.mp4", 1, 1280, 720)


def _load_wave(_sb, start, _end, _policy, _selector):
    return [_candidate(f"{start.hour:02d}-{index}", start + timedelta(minutes=index * 5)) for index in range(8)]


def _enrich(clips, **_kwargs):
    return GateEnrichment(clips, {clip.id: {"source": "fake"} for clip in clips}, {"reused": 0, "assessed": len(clips), "failed": 0})


def _select(clips, plan, _history):
    return [
        SelectedCandidate(clips[index], slot, f"episode-{plan.bucket_index}-{index}", {}, slot.value)
        for index, slot in enumerate(plan.required_slots)
    ]


def _jobs_for_day(day, *, count: int, status: str = "succeeded", completed_at: datetime | None = None):
    plans = bucket_plans(day)
    rows = []
    for index in range(count):
        plan = plans[index % 8]
        rows.append({
            "id": f"job-{day}-{index}",
            "clip_id": f"clip-{day}-{index}",
            "camera_id": "camera-a",
            "selector_version": BACKFILL_SELECTOR_VERSION,
            "window_start": plan.start.isoformat(),
            "window_end": plan.end.isoformat(),
            "slot": plan.required_slots[index % len(plan.required_slots)].value,
            "status": status,
            "attempt_count": 1,
            "error_code": None,
            "queued_at": datetime(2026, 7, 15, 0, tzinfo=timezone.utc).isoformat(),
            "completed_at": (completed_at or datetime(2026, 7, 15, tzinfo=timezone.utc)).isoformat(),
        })
    return rows


def test_target_camera_is_largest_without_hardcoded_uuid():
    start = bucket_plans(source_nights()[0])[0].start
    rows = [_motion_clip(f"a-{i}", "camera-a", start + timedelta(minutes=i)) for i in range(5)]
    rows += [_motion_clip("b-0", "camera-b", start) ]
    assert choose_target_camera(FakeSB({"motion_clips": rows}), source_nights()) == "camera-a"


def test_prepare_wave_validates_30_before_any_rpc_write():
    sb = FakeSB()
    with pytest.raises(InsufficientCandidates):
        prepare_wave(
            sb, source_nights()[0], "camera-a", load_fn=_load_wave, enrich_fn=_enrich,
            select_fn=lambda *_args: [], history_fn=lambda *_args: {},
        )
    assert sb.store.get("clip_vlm_selector_runs", []) == []
    assert sb.store.get("clip_vlm_jobs", []) == []


def test_prepare_wave_creates_8_runs_and_30_jobs_idempotently():
    sb = FakeSB()
    kwargs = dict(load_fn=_load_wave, enrich_fn=_enrich, select_fn=_select, history_fn=lambda *_args: {})
    first = prepare_wave(sb, source_nights()[0], "camera-a", **kwargs)
    second = prepare_wave(sb, source_nights()[0], "camera-a", **kwargs)
    assert len(first.selected) == len(second.selected) == 30
    assert len(sb.store["clip_vlm_selector_runs"]) == 8
    assert len(sb.store["clip_vlm_jobs"]) == 30
    assert all(job["reserved_cost_usd"] == "0" for job in sb.store["clip_vlm_jobs"])
    assert all(job["rank_features"]["source_date"] == "2026-07-07" for job in sb.store["clip_vlm_jobs"])
    assert {job["rank_features"]["bucket_index"] for job in sb.store["clip_vlm_jobs"]} == set(range(8))
    assert all(job["rank_features"]["backfill_version"] == BACKFILL_SELECTOR_VERSION for job in sb.store["clip_vlm_jobs"])
    assert all(job["rank_features"]["gate_snapshot"]["source"] == "fake" for job in sb.store["clip_vlm_jobs"])


def test_completed_wave_advances_and_partial_wave_resumes_same_date():
    days = source_nights()
    complete = FakeSB({"clip_vlm_jobs": _jobs_for_day(days[0], count=30, completed_at=datetime(2026, 7, 15, 0, tzinfo=timezone.utc))})
    assert next_source_date(complete, "camera-a", datetime(2026, 7, 15, 2, tzinfo=timezone.utc)) == days[1]
    partial = FakeSB({"clip_vlm_jobs": _jobs_for_day(days[0], count=29)})
    assert next_source_date(partial, "camera-a", datetime(2026, 7, 15, 2, tzinfo=timezone.utc)) == days[0]


def test_next_wave_waits_55_minutes_after_manual_canary():
    completed = datetime(2026, 7, 15, 0, tzinfo=timezone.utc)
    sb = FakeSB({"clip_vlm_jobs": _jobs_for_day(source_nights()[0], count=30, completed_at=completed)})
    assert next_source_date(sb, "camera-a", completed + timedelta(minutes=54)) is None
    assert next_source_date(sb, "camera-a", completed + timedelta(minutes=55)) == source_nights()[1]


def test_regular_due_jobs_defer_backfill_wave():
    regular = {"id": "regular", "selector_version": "budget-router-v1", "status": "queued", "queued_at": "2026-07-15T00:00:00+00:00"}
    sb = FakeSB({"clip_vlm_jobs": [regular]})
    calls = []
    assert run(
        sb=sb,
        process_fn=lambda _sb, jobs: calls.append([job["id"] for job in jobs]) or {"succeeded": 1},
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must defer")),
    ) == 0
    assert calls == [["regular"]]


def test_activity_worker_lock_defers_gate_prepool():
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)]})
    assert run(
        sb=sb,
        now=datetime(2026, 7, 15, 2, tzinfo=timezone.utc),
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        acquire_activity_lock_fn=lambda: None,
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must defer")),
    ) == 0


def test_previous_quota_error_blocks_future_backfill_wave():
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({
        "motion_clips": [_motion_clip("a", "camera-a", start)],
        "clip_vlm_jobs": [{
            "id": "limited",
            "camera_id": "camera-a",
            "selector_version": BACKFILL_SELECTOR_VERSION,
            "status": "failed_retryable",
            "error_code": "quota_exceeded",
        }],
    })
    assert run(
        sb=sb,
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must remain blocked")),
    ) == 0


def test_existing_30_job_wave_resumes_without_gate_or_reselection():
    jobs = _jobs_for_day(source_nights()[0], count=30, status="failed_retryable")
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)], "clip_vlm_jobs": jobs})
    calls = []
    assert run(
        sb=sb,
        process_fn=lambda _sb, due: calls.append([job["id"] for job in due]) or {"succeeded": len(due)},
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("resume must not reselect")),
        acquire_activity_lock_fn=lambda: (_ for _ in ()).throw(AssertionError("resume must not run Gate")),
    ) == 0
    assert len(calls) == 1
    assert len(calls[0]) == 30


def test_partial_persisted_wave_fails_closed_instead_of_adding_jobs():
    jobs = _jobs_for_day(source_nights()[0], count=29, status="failed_retryable")
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)], "clip_vlm_jobs": jobs})
    assert run(
        sb=sb,
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("partial wave must fail closed")),
    ) == 0
    assert len(sb.store["clip_vlm_jobs"]) == 29
