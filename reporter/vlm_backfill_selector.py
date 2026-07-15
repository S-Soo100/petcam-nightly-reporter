from __future__ import annotations

import bisect
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from reporter.vlm_episode import reduce_episodes
from reporter.vlm_models import CandidateClip, EpisodeRepresentative, SelectedCandidate, Slot
from reporter.vlm_selector import ORDER, select_candidates

KST = ZoneInfo("Asia/Seoul")
SOURCE_DATES = tuple(date(2026, 7, day) for day in range(7, 15))
BACKFILL_SELECTOR_VERSION = "budget-router-backfill-20260707-14-v1"


@dataclass(frozen=True, slots=True)
class BucketPlan:
    source_date: date
    bucket_index: int
    start: datetime
    end: datetime
    required_slots: tuple[Slot, ...]


def source_nights() -> tuple[date, ...]:
    return SOURCE_DATES


def bucket_plans(source_date: date) -> tuple[BucketPlan, ...]:
    night_index = SOURCE_DATES.index(source_date)
    night_start = datetime.combine(source_date, time(20), KST)
    plans = []
    for bucket_index in range(8):
        slots = list(ORDER)
        if bucket_index == night_index:
            slots.remove(Slot.DIVERSITY_DISCOVERY)
        elif bucket_index == (night_index + 4) % 8:
            slots.remove(Slot.EXCLUSION_AUDIT)
        start = night_start + timedelta(hours=bucket_index)
        plans.append(BucketPlan(source_date, bucket_index, start, start + timedelta(hours=1), tuple(slots)))
    return tuple(plans)


def _motion_quartiles(clips: list[CandidateClip]) -> dict[str, int]:
    values = sorted(clip.motion_score for clip in clips)
    if not values:
        return {}
    cuts = [values[max(0, (len(values) * n + 3) // 4 - 1)] for n in (1, 2, 3)]
    return {clip.id: bisect.bisect_right(cuts, clip.motion_score) for clip in clips}


def build_prepool(clips: list[CandidateClip], limit: int = 15) -> list[CandidateClip]:
    valid = sorted(
        (clip for clip in clips if clip.duration_sec > 0 and clip.r2_key and not clip.hard_skip_reason),
        key=lambda clip: (clip.started_at, clip.id),
    )
    quartiles = _motion_quartiles(valid)
    groups: dict[tuple[int, int], list[CandidateClip]] = defaultdict(list)
    for clip in valid:
        episode = int(clip.started_at.timestamp() // 1800)
        groups[(episode, quartiles[clip.id])].append(clip)
    for key in groups:
        groups[key].sort(key=lambda clip: hashlib.sha256(clip.id.encode()).hexdigest())
    ordered_keys = sorted(groups)
    selected: list[CandidateClip] = []
    offset = 0
    while len(selected) < limit:
        added = False
        for key in ordered_keys:
            if offset < len(groups[key]):
                selected.append(groups[key][offset])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        offset += 1
    return selected


def _fallback_key(slot: Slot, rep: EpisodeRepresentative, history: dict[str, int], window_start: datetime):
    clip = rep.clip
    digest = hashlib.sha256(f"{slot.value}:{window_start.isoformat()}:{clip.id}".encode()).hexdigest()
    if slot is Slot.CUSTOMER_HIGHLIGHT:
        return clip.motion_score, digest
    if slot is Slot.SUBTLE_BEHAVIOR:
        return -clip.motion_score, digest
    if slot is Slot.DIVERSITY_DISCOVERY:
        bucket = f"{clip.camera_id}|{clip.started_at.hour}|{rep.motion_quartile}|{rep.bbox_bucket}"
        return -history.get(bucket, 0), digest
    is_audit = (clip.activity_decision or "unknown") in {"exclude_absent", "exclude_static", "unknown"}
    return int(is_audit), digest


def _features(item: SelectedCandidate, plan: BucketPlan, *, fallback: bool) -> dict[str, object]:
    return {
        **item.rank_features,
        "source_date": plan.source_date.isoformat(),
        "bucket_index": plan.bucket_index,
        "backfill_version": BACKFILL_SELECTOR_VERSION,
        "fallback": fallback,
    }


def select_bucket_candidates(
    clips: list[CandidateClip], plan: BucketPlan, history: dict[str, int]
) -> list[SelectedCandidate]:
    reps = reduce_episodes(clips, plan.start)
    natural = {item.slot: item for item in select_candidates(reps, history, plan.start)}
    selected: list[SelectedCandidate] = []
    used_ids: set[str] = set()
    used_episodes: set[str] = set()
    for slot in plan.required_slots:
        item = natural.get(slot)
        if item is not None and item.clip.id not in used_ids and item.episode_key not in used_episodes:
            selected.append(SelectedCandidate(
                item.clip, item.slot, item.episode_key, _features(item, plan, fallback=False), item.selection_reason
            ))
            used_ids.add(item.clip.id)
            used_episodes.add(item.episode_key)
            continue
        available = [rep for rep in reps if rep.clip.id not in used_ids and rep.episode_key not in used_episodes]
        if not available:
            continue
        rep = max(available, key=lambda candidate: _fallback_key(slot, candidate, history, plan.start))
        base = SelectedCandidate(rep.clip, slot, rep.episode_key, {}, f"{slot.value}:fallback")
        selected.append(SelectedCandidate(
            rep.clip, slot, rep.episode_key, _features(base, plan, fallback=True), base.selection_reason
        ))
        used_ids.add(rep.clip.id)
        used_episodes.add(rep.episode_key)
    return selected
