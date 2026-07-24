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


def test_open_jobs_stable_pagination_finds_eligible_behind_many_excluded():
    # 앞의 quarantined/media_deleted job 수와 무관하게 뒤의 eligible job 을 limit 만큼 찾는다.
    # 제외 10개(queued_at 앞) + 정상 1개(뒤), limit=1 → 정상 1개를 반드시 반환한다.
    jobs = [
        {
            "id": f"job-x{i}",
            "clip_id": f"x{i}",
            "selector_version": "budget-router-v1",
            "status": "queued",
            "window_start": datetime(2026, 7, 20, 0, 30, 0, tzinfo=timezone.utc).isoformat(),
            "queued_at": datetime(2026, 7, 20, 0, 0, i, tzinfo=timezone.utc).isoformat(),
        }
        for i in range(10)
    ]
    jobs.append(
        {
            "id": "job-ok",
            "clip_id": "ok",
            "selector_version": "budget-router-v1",
            "status": "queued",
            "window_start": datetime(2026, 7, 20, 0, 30, 0, tzinfo=timezone.utc).isoformat(),
            "queued_at": datetime(2026, 7, 20, 0, 0, 59, tzinfo=timezone.utc).isoformat(),  # 맨 뒤
        }
    )
    sb = FakeSB(
        {
            "clip_vlm_jobs": jobs,
            "motion_clip_system_exclusions": [
                {"clip_id": f"x{i}", "state": "quarantined"} for i in range(10)
            ],
        }
    )
    before = datetime(2026, 7, 20, 2, 0, 0, tzinfo=timezone.utc)
    rows = _open_jobs_for_selector(sb, "budget-router-v1", before=before, limit=1)
    assert [r["id"] for r in rows] == ["job-ok"]  # 제외 10개 뒤 eligible 1개


_SAME_TS = datetime(2026, 7, 20, 0, 30, 0, tzinfo=timezone.utc).isoformat()  # 동일 queued_at


def _tsjob(job_id: str, clip_id: str, queued_at: str = _SAME_TS) -> dict:
    return {
        "id": job_id,
        "clip_id": clip_id,
        "selector_version": "budget-router-v1",
        "status": "queued",
        "window_start": datetime(2026, 7, 20, 0, 30, 0, tzinfo=timezone.utc).isoformat(),
        "queued_at": queued_at,
    }


def test_open_jobs_keyset_duplicate_queued_at_across_page_boundary_no_dup_or_skip():
    # 같은 queued_at 을 가진 job 이 페이지 경계를 걸쳐도 (queued_at ASC, id ASC) 복합 keyset 으로
    # 중복·누락 없이 순회한다. page=2 로 강제해 4개 동률 job 이 2 페이지에 걸치게 한다.
    jobs = [_tsjob("j-a", "a"), _tsjob("j-b", "b"), _tsjob("j-c", "c"), _tsjob("j-d", "d")]
    sb = FakeSB({"clip_vlm_jobs": jobs})
    before = datetime(2026, 7, 20, 2, 0, 0, tzinfo=timezone.utc)
    rows = _open_jobs_for_selector(sb, "budget-router-v1", before=before, limit=4, page=2)
    ids = [r["id"] for r in rows]
    assert ids == ["j-a", "j-b", "j-c", "j-d"]  # (queued_at,id) 순, 중복/누락 0
    assert len(ids) == len(set(ids))


def test_open_jobs_keyset_excluded_across_boundary_finds_eligible():
    # 동률 queued_at 5개 중 a,c 격리 → page=2, limit=2 여도 뒤 eligible(b,d)을 경계 넘어 찾는다.
    jobs = [_tsjob(f"j-{c}", c) for c in ("a", "b", "c", "d", "e")]
    sb = FakeSB(
        {
            "clip_vlm_jobs": jobs,
            "motion_clip_system_exclusions": [
                {"clip_id": "a", "state": "quarantined"},
                {"clip_id": "c", "state": "media_deleted"},
            ],
        }
    )
    before = datetime(2026, 7, 20, 2, 0, 0, tzinfo=timezone.utc)
    rows = _open_jobs_for_selector(sb, "budget-router-v1", before=before, limit=2, page=2)
    assert [r["id"] for r in rows] == ["j-b", "j-d"]  # a/c 제외, 경계 넘어 중복/누락 0


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
