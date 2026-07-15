from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tests._fakes import FakeSB
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION, source_nights
from reporter.vlm_backfill_summary import (
    NIGHT_TARGET,
    TOTAL_TARGET,
    aggregate_backfill_progress,
    format_backfill_progress,
    next_backfill_run,
    send_backfill_progress,
)
from reporter.vlm_candidate_worker import ProcessResult

_KST = ZoneInfo("Asia/Seoul")


def _bf_job(source_date, status):
    return {"selector_version": BACKFILL_SELECTOR_VERSION, "status": status,
            "rank_features": {"source_date": source_date}}


def _reg_job(status):
    return {"selector_version": "budget-router-v1", "status": status, "rank_features": {}}


def _stats(succeeded=0, retryable=0, failed=0, held=0):
    return ProcessResult(
        counts={"succeeded": succeeded, "failed_retryable": retryable,
                "failed_terminal": failed, "held_model_mismatch": held},
        breaker=None, job_short_ids=(), diagnostic_counts={})


def test_total_target_is_240():
    assert TOTAL_TARGET == 240 and NIGHT_TARGET == 30


def test_aggregate_counts_only_backfill_selector():
    rows = ([_bf_job("2026-07-07", "succeeded") for _ in range(30)]
            + [_bf_job("2026-07-08", "succeeded") for _ in range(28)]
            + [_reg_job("succeeded") for _ in range(15)])  # 정규 selector 는 제외돼야 함
    sb = FakeSB({"clip_vlm_jobs": rows})
    summary = aggregate_backfill_progress(
        sb, source_date="2026-07-08", host="baeg-endeuui-Macmini.local",
        now=datetime(2026, 7, 16, 10, tzinfo=timezone.utc),
        this_run_stats=_stats(succeeded=28, retryable=2), processed_target=30)
    assert summary.cumulative_succeeded == 58  # 정규 15 미포함
    assert summary.remaining == TOTAL_TARGET - 58
    assert summary.this_run == {"target": 30, "succeeded": 28, "retryable": 2, "failed": 0, "held": 0}


def test_format_progress_message_shape():
    sb = FakeSB({"clip_vlm_jobs": [_bf_job("2026-07-09", "succeeded") for _ in range(105)]})
    summary = aggregate_backfill_progress(
        sb, source_date="2026-07-09", host="baeg-endeuui-Macmini.local",
        now=datetime(2026, 7, 16, 4, tzinfo=timezone.utc),  # KST 13:00 daytime
        this_run_stats=_stats(succeeded=28, retryable=2), processed_target=30)
    msg = format_backfill_progress(summary)
    assert "📦 과거 영상 VLM 분석" in msg
    assert "· 실행 장비: Mac mini" in msg
    assert "· 처리 날짜: 07/09" in msg
    assert "· 이번 실행: 대상 30 · 성공 28 · 재시도 2 · 실패 0" in msg
    assert "· 누적: 완료 105 / 전체 240" in msg
    assert "· 남은 영상: 135" in msg
    assert "예상 완료:" in msg and "다음 실행:" in msg
    # raw 노출 없음
    for secret in ("/Users/", "@", "reasoning"):
        assert secret not in msg


def test_completion_message_once_when_all_dates_done():
    rows = []
    for d in source_nights():
        rows += [_bf_job(d.isoformat(), "succeeded") for _ in range(30)]
    sb = FakeSB({"clip_vlm_jobs": rows})
    summary = aggregate_backfill_progress(
        sb, source_date=source_nights()[-1], host="Mac mini",
        now=datetime(2026, 7, 16, 10, tzinfo=timezone.utc),
        this_run_stats=_stats(succeeded=30), processed_target=30)
    assert summary.complete is True and summary.remaining == 0
    msg = format_backfill_progress(summary)
    assert "전체 완료" in msg
    assert "남은 영상: 0" in msg


def test_next_backfill_run_daytime_and_night():
    assert next_backfill_run(datetime(2026, 7, 15, 12, tzinfo=_KST)) == datetime(2026, 7, 15, 13, tzinfo=_KST)
    assert next_backfill_run(datetime(2026, 7, 15, 3, tzinfo=_KST)) == datetime(2026, 7, 15, 7, tzinfo=_KST)
    assert next_backfill_run(datetime(2026, 7, 15, 22, tzinfo=_KST)) == datetime(2026, 7, 16, 7, tzinfo=_KST)


def test_send_progress_swallows_slack_failure():
    sb = FakeSB({"clip_vlm_jobs": [_bf_job("2026-07-09", "succeeded")]})
    summary = aggregate_backfill_progress(
        sb, source_date="2026-07-09", host="Mac mini",
        now=datetime(2026, 7, 16, 4, tzinfo=timezone.utc),
        this_run_stats=_stats(succeeded=1), processed_target=1)

    def boom(_t):
        raise RuntimeError("down")
    assert send_backfill_progress(summary, send_fn=boom) is False
    sent = []
    assert send_backfill_progress(summary, send_fn=lambda t: sent.append(t) or True) is True
    assert len(sent) == 1
