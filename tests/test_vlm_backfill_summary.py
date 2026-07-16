from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tests._fakes import FakeSB
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION
from reporter.vlm_backfill_summary import (
    aggregate_backfill_progress,
    format_backfill_progress,
    next_backfill_run,
    send_backfill_progress,
)
from reporter.vlm_candidate_worker import ProcessResult

_KST = ZoneInfo("Asia/Seoul")


def _bf_job(status):
    return {"selector_version": BACKFILL_SELECTOR_VERSION, "status": status}


def _reg_job(status):
    return {"selector_version": "budget-router-v1", "status": status}


def _stats(succeeded=0, retryable=0, failed=0, held=0):
    return ProcessResult(
        counts={"succeeded": succeeded, "failed_retryable": retryable,
                "failed_terminal": failed, "held_model_mismatch": held},
        breaker=None, job_short_ids=(), diagnostic_counts={})


def test_aggregate_rolling_counts_only_backfill_selector():
    rows = ([_bf_job("succeeded") for _ in range(124)]
            + [_bf_job("failed_terminal") for _ in range(18)]
            + [_bf_job("queued") for _ in range(4)]
            + [_reg_job("succeeded") for _ in range(15)])  # 정규 selector 제외
    sb = FakeSB({"clip_vlm_jobs": rows})
    s = aggregate_backfill_progress(
        sb, source_date="2026-07-10", host="baeg-endeuui-Macmini.local",
        now=datetime(2026, 7, 16, 1, tzinfo=timezone.utc),  # KST 10:00
        this_run_stats=_stats(succeeded=24, retryable=4, failed=2), processed_target=30, waiting_dates=3)
    assert s.created == 146 and s.succeeded == 124 and s.terminal == 18 and s.in_progress == 4
    assert s.processed == 142       # succeeded + terminal, 240 프레이밍 없음
    assert s.waiting_dates == 3
    assert s.this_run == {"target": 30, "succeeded": 24, "retryable": 4, "failed": 2, "held": 0}


def test_format_rolling_message_shape_no_fixed_240():
    sb = FakeSB({"clip_vlm_jobs": [_bf_job("succeeded") for _ in range(124)]
                                   + [_bf_job("failed_terminal") for _ in range(18)]
                                   + [_bf_job("queued") for _ in range(4)]})
    s = aggregate_backfill_progress(
        sb, source_date="2026-07-10", host="baeg-endeuui-Macmini.local",
        now=datetime(2026, 7, 16, 1, tzinfo=timezone.utc),  # KST 10:00 → 다음 10:35
        this_run_stats=_stats(succeeded=24, retryable=4, failed=2), processed_target=30, waiting_dates=3)
    msg = format_backfill_progress(s)
    assert "📦 과거 영상 VLM 분석" in msg
    assert "· 실행 장비: Mac mini" in msg
    assert "· 처리 날짜: 07/10" in msg
    assert "· 이번 실행: 대상 30 · 성공 24 · 재시도 4 · 영구실패 2" in msg
    assert "· 누적 처리: 142 (성공 124 · 영구실패 18)" in msg
    assert "· 현재 backlog: 진행 중 4 · 대기 날짜 3일" in msg
    assert "· 다음 실행: 07/16 10:35" in msg
    assert "· 정규 VLM 보호: 정상" in msg
    assert "240" not in msg  # 고정 전체 목표 제거
    for secret in ("/Users/", "@", "reasoning"):
        assert secret not in msg


def test_next_backfill_run_is_next_35():
    assert next_backfill_run(datetime(2026, 7, 16, 10, 0, tzinfo=_KST)) == datetime(2026, 7, 16, 10, 35, tzinfo=_KST)
    assert next_backfill_run(datetime(2026, 7, 16, 10, 40, tzinfo=_KST)) == datetime(2026, 7, 16, 11, 35, tzinfo=_KST)


def test_send_progress_swallows_slack_failure():
    sb = FakeSB({"clip_vlm_jobs": [_bf_job("succeeded")]})
    s = aggregate_backfill_progress(
        sb, source_date="2026-07-10", host="Mac mini",
        now=datetime(2026, 7, 16, 1, tzinfo=timezone.utc),
        this_run_stats=_stats(succeeded=1), processed_target=1, waiting_dates=0)

    def boom(_t):
        raise RuntimeError("down")
    assert send_backfill_progress(s, send_fn=boom) is False
    sent = []
    assert send_backfill_progress(s, send_fn=lambda t: sent.append(t) or True) is True
    assert len(sent) == 1
