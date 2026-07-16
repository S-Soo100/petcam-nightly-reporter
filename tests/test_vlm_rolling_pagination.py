from tests._fakes import FakeSB
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION
from reporter.vlm_backfill_worker import _job_state_by_date
from reporter.vlm_store import load_backfill_ledger, load_dedup_clip_ids


def test_dedup_clip_ids_paginates_beyond_1000():
    jobs = [{"id": f"job-{i:05d}", "clip_id": f"clip-{i}",
             "selector_version": "budget-router-v1", "status": "succeeded"} for i in range(1001)]
    sb = FakeSB({"clip_vlm_jobs": jobs})
    ids = load_dedup_clip_ids(sb)
    assert len(ids) == 1001
    assert "clip-1000" in ids  # 1000행 이후 오래된 clip 도 재선정 방지 대상


def test_job_state_paginates_beyond_1000_no_source_date_missing():
    jobs = []
    for i in range(1001):
        d = "2026-07-07" if i < 500 else "2026-07-08"
        jobs.append({"id": f"job-{i:05d}", "clip_id": f"c{i}", "selector_version": BACKFILL_SELECTOR_VERSION,
                     "status": "succeeded", "rank_features": {"source_date": d}})
    sb = FakeSB({"clip_vlm_jobs": jobs})
    js = _job_state_by_date(sb)
    assert js["2026-07-07"]["created"] == 500
    assert js["2026-07-08"]["created"] == 501  # 1000행 이후 과거 source_date 상태 누락 없음


def test_ledger_paginates_beyond_1000():
    rows = [{"id": f"led-{i:05d}", "selector_version": BACKFILL_SELECTOR_VERSION,
             "source_date": f"2026-{(i % 12) + 1:02d}-01", "scope": "cam", "status": "completed"} for i in range(1001)]
    sb = FakeSB({"vlm_backfill_ledger": rows})
    assert len(load_backfill_ledger(sb, BACKFILL_SELECTOR_VERSION)) == 1001
