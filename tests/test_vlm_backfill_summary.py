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


def test_aggregate_semantics_processed_vs_remaining_excludes_terminal():
    # 90 created: 78 succeeded + 12 terminal, open 0 → 처리 90, 남은 처리 240-90=150
    rows = ([_bf_job("2026-07-07", "succeeded") for _ in range(78)]
            + [_bf_job("2026-07-08", "failed_terminal") for _ in range(12)]
            + [_reg_job("succeeded") for _ in range(15)])  # 정규 selector 제외
    sb = FakeSB({"clip_vlm_jobs": rows})
    summary = aggregate_backfill_progress(
        sb, source_date="2026-07-09", host="baeg-endeuui-Macmini.local",
        now=datetime(2026, 7, 16, 10, tzinfo=timezone.utc),
        this_run_stats=_stats(succeeded=4, retryable=2), processed_target=30)
    assert summary.created == 90 and summary.succeeded == 78 and summary.terminal == 12
    assert summary.processed == 90                       # succeeded + terminal
    assert summary.remaining_processing == 150           # terminal 은 남은 처리에 미포함
    assert summary.not_created == 150                     # 240 - 90 created
    assert summary.in_progress == 0
    assert summary.this_run == {"target": 30, "succeeded": 4, "retryable": 2, "failed": 0, "held": 0}


def test_format_progress_message_shape_with_processed_semantics():
    rows = ([_bf_job("2026-07-10", "succeeded") for _ in range(100)]
            + [_bf_job("2026-07-10", "failed_terminal") for _ in range(12)]
            + [_bf_job("2026-07-10", "queued") for _ in range(8)])  # created 120, processed 112, open 8
    sb = FakeSB({"clip_vlm_jobs": rows})
    summary = aggregate_backfill_progress(
        sb, source_date="2026-07-10", host="baeg-endeuui-Macmini.local",
        now=datetime(2026, 7, 16, 4, tzinfo=timezone.utc),  # KST 13:00 daytime
        this_run_stats=_stats(succeeded=28, retryable=2), processed_target=30)
    msg = format_backfill_progress(summary)
    assert "📦 과거 영상 VLM 분석" in msg
    assert "· 실행 장비: Mac mini" in msg
    assert "· 처리 날짜: 07/10" in msg
    assert "· 이번 실행: 대상 30 · 성공 28 · 재시도 2 · 실패 0" in msg
    assert "· 누적 처리: 112/240 (성공 100 · 영구실패 12)" in msg
    assert "· 진행 중: 8 · 미생성: 120" in msg
    assert "· 남은 처리: 128" in msg  # 240 - 112
    assert "예상 완료:" in msg and "다음 실행:" in msg
    for secret in ("/Users/", "@", "reasoning"):
        assert secret not in msg


def test_completion_message_when_processed_reaches_target():
    # 240 created: 228 succeeded + 12 terminal, open 0 → 처리 240/240, 남은 처리 0
    rows = ([_bf_job(source_nights()[0].isoformat(), "succeeded") for _ in range(228)]
            + [_bf_job(source_nights()[0].isoformat(), "failed_terminal") for _ in range(12)])
    sb = FakeSB({"clip_vlm_jobs": rows})
    summary = aggregate_backfill_progress(
        sb, source_date=source_nights()[-1], host="Mac mini",
        now=datetime(2026, 7, 16, 10, tzinfo=timezone.utc),
        this_run_stats=_stats(succeeded=30), processed_target=30)
    assert summary.complete is True and summary.remaining_processing == 0
    msg = format_backfill_progress(summary)
    assert "전체 완료" in msg
    assert "· 처리: 240/240 (성공 228 · 영구실패 12)" in msg
    assert "남은 처리: 0" in msg


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
