"""VLM 소비 가드 계약 테스트 (설계 §6). quarantined|media_deleted 는 새 VLM work 전에 제외.

- regular window candidates (load_window_candidates)
- regular due/recovery jobs (_open_jobs_for_selector)
- candidate|restored|deletion_blocked 는 차단 안 함
- 기존 clip_vlm_jobs 는 read-only(update/delete 0)
- bounded chunk (>1000 clip id 도 누락 없이)
실 DB 무의존(FakeSB).
"""

from __future__ import annotations

from datetime import datetime, timezone

from reporter.short_clip_retention_store import load_system_excluded_clip_ids
from reporter.vlm_candidate_indexer import load_window_candidates
from reporter.vlm_store import _open_jobs_for_selector
from tests._fakes import FakeSB

START = datetime(2026, 7, 20, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 7, 20, 1, 0, 0, tzinfo=timezone.utc)


def _clip(cid: str, minute: int) -> dict:
    return {
        "id": cid,
        "camera_id": "camera-a",
        "started_at": datetime(2026, 7, 20, 0, minute, 0, tzinfo=timezone.utc).isoformat(),
        "duration_sec": 30,
        "r2_key": f"terra-clips/clips/{cid}.mp4",
        "motion_score": 1,
        "width": 1280,
        "height": 720,
    }


# ── load_window_candidates: quarantined/media_deleted 제외, candidate/restored/deletion_blocked 유지 ──
def test_window_candidates_exclude_quarantined_and_media_deleted():
    sb = FakeSB(
        {
            "motion_clips": [_clip("c1", 1), _clip("c2", 2), _clip("c3", 3), _clip("c4", 4), _clip("c5", 5)],
            "motion_clip_system_exclusions": [
                {"clip_id": "c1", "state": "quarantined"},
                {"clip_id": "c2", "state": "media_deleted"},
                {"clip_id": "c3", "state": "candidate"},
                {"clip_id": "c4", "state": "restored"},
                # c5 = deletion_blocked, exclusion 있지만 VLM 차단 아님
                {"clip_id": "c5", "state": "deletion_blocked"},
            ],
        }
    )
    out = load_window_candidates(sb, START, END, "activity-v1", "budget-router-v1")
    ids = {c.id for c in out}
    assert ids == {"c3", "c4", "c5"}  # quarantined(c1)/media_deleted(c2) 만 제외


def test_window_candidates_no_exclusion_table_is_noop():
    # 격리 테이블이 비어 있으면(=migration 전 shadow) 후보를 하나도 안 지운다.
    sb = FakeSB({"motion_clips": [_clip("c1", 1), _clip("c2", 2)]})
    ids = {c.id for c in load_window_candidates(sb, START, END, "activity-v1", "budget-router-v1")}
    assert ids == {"c1", "c2"}


# ── _open_jobs_for_selector: 격리 clip 의 due/recovery job 제외 ──
def _job(cid: str, *, window_min: int = 30) -> dict:
    return {
        "id": f"job-{cid}",
        "clip_id": cid,
        "selector_version": "budget-router-v1",
        "status": "queued",
        "window_start": datetime(2026, 7, 20, 0, window_min, 0, tzinfo=timezone.utc).isoformat(),
        "queued_at": datetime(2026, 7, 20, 0, window_min, 0, tzinfo=timezone.utc).isoformat(),
    }


def test_open_jobs_exclude_quarantined_clip_jobs():
    sb = FakeSB(
        {
            "clip_vlm_jobs": [_job("a"), _job("b"), _job("c")],
            "motion_clip_system_exclusions": [
                {"clip_id": "a", "state": "quarantined"},
                {"clip_id": "b", "state": "media_deleted"},
                {"clip_id": "c", "state": "candidate"},
            ],
        }
    )
    before = datetime(2026, 7, 20, 2, 0, 0, tzinfo=timezone.utc)
    rows = _open_jobs_for_selector(sb, "budget-router-v1", before=before, limit=10)
    assert {r["clip_id"] for r in rows} == {"c"}  # a/b 제외, c(candidate) 유지


def test_open_jobs_does_not_mutate_existing_rows():
    jobs = [_job("a"), _job("b")]
    sb = FakeSB(
        {
            "clip_vlm_jobs": [dict(j) for j in jobs],
            "motion_clip_system_exclusions": [{"clip_id": "a", "state": "quarantined"}],
        }
    )
    before = datetime(2026, 7, 20, 2, 0, 0, tzinfo=timezone.utc)
    _open_jobs_for_selector(sb, "budget-router-v1", before=before, limit=10)
    # 기존 job row 는 그대로(상태/카운트 변화 0, 삭제 0).
    stored = sb.store["clip_vlm_jobs"]
    assert len(stored) == 2
    assert all(r["status"] == "queued" for r in stored)


# ── bounded chunk: >1000 clip id 도 누락 없이 ──
def test_load_system_excluded_chunks_over_1000_without_omission():
    ids = [f"clip-{i:05d}" for i in range(2500)]
    sb = FakeSB(
        {"motion_clip_system_exclusions": [{"clip_id": cid, "state": "quarantined"} for cid in ids]}
    )
    excluded = load_system_excluded_clip_ids(sb, ids)
    assert excluded == set(ids)  # 13 chunk 전부 반영


def test_load_system_excluded_only_blocking_states():
    sb = FakeSB(
        {
            "motion_clip_system_exclusions": [
                {"clip_id": "q", "state": "quarantined"},
                {"clip_id": "d", "state": "media_deleted"},
                {"clip_id": "c", "state": "candidate"},
                {"clip_id": "r", "state": "restored"},
                {"clip_id": "b", "state": "deletion_blocked"},
            ]
        }
    )
    assert load_system_excluded_clip_ids(sb, ["q", "d", "c", "r", "b"]) == {"q", "d"}


def test_load_system_excluded_empty_input():
    assert load_system_excluded_clip_ids(FakeSB(), []) == set()
