import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tests._fakes import FakeSB
from reporter.vlm_run_summary import (
    VlmRunSummary,
    aggregate_vlm_run,
    format_vlm_run_summary,
    next_scheduled_run,
    send_vlm_run_summary,
)

_KST = ZoneInfo("Asia/Seoul")


def _summary(**over):
    base = dict(
        window_start=datetime(2026, 7, 16, 0, tzinfo=_KST),
        window_end=datetime(2026, 7, 16, 2, tzinfo=_KST),
        host="Mac mini", run_id="20260716T0200",
        next_run=datetime(2026, 7, 16, 4, tzinfo=_KST),
        candidate_count=4, slot_counts={"customer_highlight": 1, "subtle_behavior": 1, "diversity_discovery": 1, "exclusion_audit": 1},
        status_counts={"succeeded": 4, "failed_retryable": 0, "failed_terminal": 0, "held_model_mismatch": 0, "queued": 0},
        action_dist={"moving": 2, "unseen": 2}, provider="claude_cli_batch",
        model_actual="claude-sonnet-5", model_mismatch_count=0, direct_api_cost_usd=0.0,
        oldest_due_age_min=0,
    )
    base.update(over)
    return VlmRunSummary(**base)


def test_format_happy_path_matches_contract():
    msg = format_vlm_run_summary(_summary())
    assert "🦎 VLM 후보 분석 (07/16 00:00~02:00 KST)" in msg
    assert "· host: Mac mini · run: 20260716T0200" in msg
    assert "· 후보 4개: 하이라이트 1 / 미세행동 1 / 다양성 1 / 제외감사 1" in msg
    assert "· 결과: 성공 4 / 재시도 0 / 실패 0 / 모델보류 0 / 대기 0" in msg
    assert "· 행동: moving 2 / unseen 2" in msg
    assert "· 모델: claude-sonnet-5 · Claude 구독 · 직접 API $0" in msg
    assert "· queue: 정상 (최고 0분) · 다음 04:00" in msg


def test_format_zero_candidates_is_not_no_special_behavior():
    msg = format_vlm_run_summary(_summary(candidate_count=0, status_counts={}, action_dist={}, slot_counts={}))
    assert "후보 0개 · VLM 호출 0회 · 정상 종료" in msg
    assert "특이행동 없음" not in msg


def test_format_partial_r2_frame_failure():
    msg = format_vlm_run_summary(_summary(
        status_counts={"succeeded": 2, "failed_retryable": 2, "failed_terminal": 0, "held_model_mismatch": 0, "queued": 0}))
    assert "성공 2 / 재시도 2" in msg


def test_format_auth_breaker_shows_queue_and_retry():
    msg = format_vlm_run_summary(_summary(
        status_counts={"succeeded": 0, "failed_retryable": 4, "failed_terminal": 0, "held_model_mismatch": 0, "queued": 0},
        action_dist={}, model_actual=None, oldest_due_age_min=42))
    assert "재시도 4" in msg
    assert "⚠️지연 (최고 42분>0" not in msg  # 형식은 '(최고 42분)'
    assert "⚠️지연 (최고 42분)" in msg


def test_format_model_mismatch_warning():
    msg = format_vlm_run_summary(_summary(
        status_counts={"succeeded": 0, "failed_retryable": 0, "failed_terminal": 0, "held_model_mismatch": 4, "queued": 0},
        model_mismatch_count=4, action_dist={}))
    assert "모델보류 4" in msg
    assert "⚠️모델불일치 4" in msg


def test_format_queue_over_30_minutes_flags_delay():
    msg = format_vlm_run_summary(_summary(oldest_due_age_min=45,
        status_counts={"succeeded": 0, "failed_retryable": 1, "failed_terminal": 0, "held_model_mismatch": 0, "queued": 3}))
    assert "⚠️지연 (최고 45분)" in msg


def test_format_never_leaks_raw_reasoning_path_uuid_email_token():
    msg = format_vlm_run_summary(_summary(
        action_dist={"moving": 1, "other": 1}))  # unknown action collapses to 'other'
    for secret in ("/Users/", "@", "sk-", "reasoning", "3f8b2c1a-"):
        assert secret not in msg


def test_next_scheduled_run_handles_kst_day_boundary():
    assert next_scheduled_run(datetime(2026, 12, 31, 22, tzinfo=_KST)) == datetime(2027, 1, 1, 0, tzinfo=_KST)
    assert next_scheduled_run(datetime(2026, 7, 16, 0, tzinfo=_KST)) == datetime(2026, 7, 16, 2, tzinfo=_KST)
    assert next_scheduled_run(datetime(2026, 7, 16, 2, tzinfo=_KST)) == datetime(2026, 7, 16, 4, tzinfo=_KST)
    assert next_scheduled_run(datetime(2026, 7, 16, 4, tzinfo=_KST)) == datetime(2026, 7, 16, 22, tzinfo=_KST)


def test_format_blocked_lock_warns_and_reports_zero_claude():
    msg = format_vlm_run_summary(_summary(blocked_lock=True))
    assert "blocked_lock" in msg
    assert "Claude 호출 0회" in msg


# --- aggregation (Step 3) ---

def _job(status, slot, window_start, *, model_actual=None, result=None, cost="0", queued_at="2026-07-15T17:00:00+00:00", selector="budget-router-v1"):
    return {"status": status, "slot": slot, "selector_version": selector, "window_start": window_start,
            "model_actual": model_actual, "result": result, "cost_usd": cost, "queued_at": queued_at}


def test_aggregate_only_counts_regular_selector_window_and_drops_reasoning():
    start = datetime(2026, 7, 15, 15, tzinfo=timezone.utc)  # KST 00:00
    end = datetime(2026, 7, 15, 17, tzinfo=timezone.utc)    # KST 02:00
    ws = start.isoformat()
    rows = [
        _job("succeeded", "customer_highlight", ws, model_actual="claude-sonnet-5",
             result={"action": "moving", "reasoning": "secret at /Users/x uuid 3f8b2c1a-0000-1111-2222-333344445555"}),
        _job("succeeded", "subtle_behavior", ws, model_actual="claude-sonnet-5", result={"action": "unseen", "reasoning": "r"}),
        _job("failed_retryable", "diversity_discovery", ws),
        # backfill job in same window must be excluded
        _job("succeeded", "exclusion_audit", ws, model_actual="claude-sonnet-5", result={"action": "moving"}, selector="budget-router-backfill-20260707-14-v1"),
    ]
    sb = FakeSB({"clip_vlm_jobs": rows})
    now = datetime(2026, 7, 15, 17, 5, tzinfo=timezone.utc)
    summary = aggregate_vlm_run(
        sb, "budget-router-v1", start, end, now=now, host="Mac mini", run_id="r1",
        next_run=datetime(2026, 7, 15, 19, tzinfo=timezone.utc), model_expected="claude-sonnet-5")
    assert summary.candidate_count == 3  # backfill 제외
    assert summary.status_counts["succeeded"] == 2
    assert summary.action_dist == {"moving": 1, "unseen": 1}
    assert summary.model_actual == "claude-sonnet-5" and summary.model_mismatch_count == 0
    # reasoning/path/uuid 는 요약 어디에도 없음
    assert "/Users/x" not in json.dumps(summary.action_dist)
    assert "3f8b2c1a" not in format_vlm_run_summary(summary)


def test_aggregate_oldest_due_age_uses_regular_open_jobs():
    start = datetime(2026, 7, 15, 15, tzinfo=timezone.utc)
    end = datetime(2026, 7, 15, 17, tzinfo=timezone.utc)
    ws = start.isoformat()
    rows = [_job("failed_retryable", "customer_highlight", ws, queued_at="2026-07-15T16:00:00+00:00")]
    sb = FakeSB({"clip_vlm_jobs": rows})
    now = datetime(2026, 7, 15, 16, 40, tzinfo=timezone.utc)  # 40분 경과
    summary = aggregate_vlm_run(sb, "budget-router-v1", start, end, now=now, host="Mac mini", run_id="r1",
                                next_run=datetime(2026, 7, 15, 19, tzinfo=timezone.utc))
    assert summary.oldest_due_age_min == 40


def test_send_summary_returns_false_on_send_failure_without_raising():
    def boom(_text):
        raise RuntimeError("slack down")
    assert send_vlm_run_summary(_summary(), send_fn=boom) is False
    sent = []
    assert send_vlm_run_summary(_summary(), send_fn=lambda text: sent.append(text) or True) is True
    assert len(sent) == 1
