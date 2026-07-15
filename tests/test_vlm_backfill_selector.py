from collections import Counter
from datetime import datetime, timedelta, timezone

from reporter.vlm_models import CandidateClip, Slot
from reporter.vlm_backfill_selector import (
    bucket_plans,
    build_prepool,
    select_bucket_candidates,
    source_nights,
)


def _clip(index: int, *, decision: str | None = None) -> CandidateClip:
    return CandidateClip(
        id=f"clip-{index:03d}",
        camera_id="camera-a",
        started_at=datetime(2026, 7, 7, 11, tzinfo=timezone.utc) + timedelta(seconds=index * 40),
        duration_sec=30,
        r2_key=f"clips/{index}.mp4",
        motion_score=float((index % 20) + 1),
        width=1280,
        height=720,
        activity_decision=decision,
    )


def test_eight_nights_are_240_and_each_hour_is_30():
    plans = [plan for day in source_nights() for plan in bucket_plans(day)]
    assert len(plans) == 64
    assert sum(len(plan.required_slots) for plan in plans) == 240
    assert [
        sum(len(plan.required_slots) for plan in plans if plan.bucket_index == bucket)
        for bucket in range(8)
    ] == [30] * 8


def test_each_night_has_8_8_7_7_slots():
    expected = {
        Slot.CUSTOMER_HIGHLIGHT: 8,
        Slot.SUBTLE_BEHAVIOR: 8,
        Slot.DIVERSITY_DISCOVERY: 7,
        Slot.EXCLUSION_AUDIT: 7,
    }
    for day in source_nights():
        counts = Counter(slot for plan in bucket_plans(day) for slot in plan.required_slots)
        assert counts == expected


def test_prepool_is_deterministic_and_at_most_15():
    clips = [_clip(index) for index in range(80)]
    forward = build_prepool(clips)
    reverse = build_prepool(list(reversed(clips)))
    assert forward == reverse
    assert 4 <= len(forward) <= 15
    assert len({clip.id for clip in forward}) == len(forward)


def test_missing_natural_slots_use_annotated_fallbacks():
    plan = bucket_plans(source_nights()[0])[1]
    clips = [_clip(index) for index in range(15)]
    selected = select_bucket_candidates(clips, plan, {})
    assert len(selected) == 4
    assert {item.slot for item in selected} == set(plan.required_slots)
    assert len({item.clip.id for item in selected}) == 4
    assert any("fallback" in item.selection_reason for item in selected)
    assert all(item.rank_features["source_date"] == "2026-07-07" for item in selected)
