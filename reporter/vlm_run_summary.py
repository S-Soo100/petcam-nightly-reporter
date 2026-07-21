"""VLM 전용 Slack 요약(설계 §9). metadata 상황판과 혼동되지 않게 정규 scheduled run 마다
후보 수·결과·행동 분포·실제 모델·비용·queue 지연·다음 실행을 1회 보고한다.

금지: raw model reasoning / stdout·stderr / email·token·전체 path·전체 UUID /
'특이행동 없음'을 후보 0/분석 0 의 의미로 사용.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_SLOT_ORDER = ("customer_highlight", "subtle_behavior", "diversity_discovery", "exclusion_audit")
_SLOT_LABEL = {
    "customer_highlight": "하이라이트", "subtle_behavior": "미세행동",
    "diversity_discovery": "다양성", "exclusion_audit": "제외감사",
}
# v4.0 행동 클래스 → 운영자용 한글 라벨. allowlist 밖 action 은 raw 노출 대신 기타(other)로 묶는다.
_ACTION_ALLOWLIST = ("eating_paste", "eating_prey", "drinking", "shedding", "basking", "moving", "unseen", "hand_feeding")
_ACTION_LABEL = {
    "eating_paste": "페이스트 섭취", "eating_prey": "먹이 섭취", "drinking": "핥기",
    "shedding": "탈피", "basking": "휴식", "moving": "일반이동", "unseen": "게코 안 보임",
    "hand_feeding": "손급여", "other": "기타",
}
_STATUS_ORDER = ("succeeded", "failed_retryable", "failed_terminal", "held_model_mismatch")
_STATUS_LABEL = {
    "succeeded": "성공", "failed_retryable": "재시도", "failed_terminal": "실패",
    "held_model_mismatch": "모델보류", "queued": "대기",
}


@dataclass(frozen=True, slots=True)
class VlmRunSummary:
    window_start: datetime
    window_end: datetime
    host: str
    run_id: str
    next_run: datetime
    candidate_count: int = 0
    slot_counts: dict = field(default_factory=dict)
    status_counts: dict = field(default_factory=dict)
    action_dist: dict = field(default_factory=dict)
    provider: str = "claude_cli_batch"
    model_actual: str | None = None
    model_mismatch_count: int = 0
    direct_api_cost_usd: float = 0.0
    oldest_due_age_min: int = 0
    recovery_count: int = 0
    blocked_lock: bool = False


def next_scheduled_run(now: datetime) -> datetime:
    """22→00→02→04→(익일)22 순환에서 다음 정규 실행 시각(KST). year/month/day 경계 반영."""
    kst = now.astimezone(KST).replace(minute=0, second=0, microsecond=0)
    if kst.hour == 22:
        return (kst + timedelta(days=1)).replace(hour=0)
    if kst.hour == 0:
        return kst.replace(hour=2)
    if kst.hour == 2:
        return kst.replace(hour=4)
    return kst.replace(hour=22)  # 04:00 → 같은 날 22:00


def _action_label(action: str) -> str:
    return action if action in _ACTION_ALLOWLIST else "other"


def aggregate_vlm_run(sb, selector_version, start, end, *, now, host, run_id, next_run,
                      recovery_count=0, provider="claude_cli_batch", model_expected=None,
                      blocked_lock=False) -> VlmRunSummary:
    """이 scheduled invocation 의 정규 selector job 만 집계한다(backfill 제외)."""
    if blocked_lock:
        return VlmRunSummary(window_start=start, window_end=end, host=host, run_id=run_id,
                             next_run=next_run, provider=provider, blocked_lock=True)
    rows = (sb.table("clip_vlm_jobs").select("*").eq("selector_version", selector_version)
            .gte("window_start", start.isoformat()).lt("window_start", end.isoformat()).execute().data)
    status_counts = Counter(r.get("status") for r in rows)
    slot_counts = Counter(r.get("slot") for r in rows)
    succeeded = [r for r in rows if r.get("status") == "succeeded"]
    action_dist = Counter(_action_label((r.get("result") or {}).get("action", "other")) for r in succeeded)
    models = {r.get("model_actual") for r in succeeded if r.get("model_actual")}
    model_actual = sorted(models)[0] if models else None
    mismatch = status_counts.get("held_model_mismatch", 0)
    if model_expected is not None:
        mismatch += sum(1 for m in models if m != model_expected)
    open_rows = [r for r in rows if r.get("status") in ("queued", "failed_retryable")]
    oldest_min = 0
    if open_rows:
        oldest = min(_parse(r.get("queued_at")) for r in open_rows if r.get("queued_at"))
        oldest_min = max(0, int((now - oldest).total_seconds() // 60))
    cost = sum(float(r.get("cost_usd") or 0) for r in rows)
    return VlmRunSummary(
        window_start=start, window_end=end, host=host, run_id=run_id, next_run=next_run,
        candidate_count=len(rows), slot_counts=dict(slot_counts), status_counts=dict(status_counts),
        action_dist=dict(action_dist), provider=provider, model_actual=model_actual,
        model_mismatch_count=mismatch, direct_api_cost_usd=cost, oldest_due_age_min=oldest_min,
        recovery_count=recovery_count, blocked_lock=False,
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def friendly_host(host: str | None) -> str:
    """운영자용 장비 별칭. 'baeg-endeuui-Macmini.local' → 'Mac mini'."""
    normalized = (host or "").lower().replace(" ", "")
    if "macmini" in normalized:
        return "Mac mini"
    if "macbook" in normalized:
        return "MacBook"
    return host or "unknown"


def _run_short(run_id: str) -> str:
    """추적용 짧은 run id. '20260716T020000' → '0200'."""
    if "T" in run_id and len(run_id) >= 13:
        return run_id[9:13]
    return run_id[-4:] if run_id else "----"


def _model_label(model_actual: str | None) -> str:
    return "Claude Sonnet 5" if model_actual == "claude-sonnet-5" else (model_actual or "—")


def format_vlm_run_summary(s: VlmRunSummary) -> str:
    ws = s.window_start.astimezone(KST)
    we = s.window_end.astimezone(KST)
    nxt = s.next_run.astimezone(KST)
    head = f"🦎 VLM 행동 분석 ({ws:%H:%M}~{we:%H:%M})"
    hostline = f"· 실행 장비: {friendly_host(s.host)} · run {_run_short(s.run_id)}"
    if s.blocked_lock:
        return "\n".join([head, hostline,
                          f"· ⚠️ 실행 충돌(blocked_lock): 다른 실행이 lock 점유 · Claude 호출 0회 · 다음 분석 {nxt:%H:%M}"])
    if s.candidate_count == 0:
        return "\n".join([head, hostline, f"· 후보 0개 · Claude 호출 0회 · 정상 종료 · 다음 분석 {nxt:%H:%M}"])
    queued = s.status_counts.get("queued", 0)
    analyzed = max(0, s.candidate_count - queued)
    cand = f"· 후보: {s.candidate_count}개 / 실제 분석: {analyzed}개"
    slot = "· 선정: " + " · ".join(f"{_SLOT_LABEL[k]} {s.slot_counts.get(k, 0)}" for k in _SLOT_ORDER)
    result_parts = [f"{_STATUS_LABEL[k]} {s.status_counts.get(k, 0)}" for k in _STATUS_ORDER]
    if queued:
        result_parts.append(f"대기 {queued}")
    if s.recovery_count:
        result_parts.append(f"복구 {s.recovery_count}")
    result = "· 결과: " + " · ".join(result_parts)
    ordered = sorted(s.action_dist.items(), key=lambda kv: (_ACTION_ALLOWLIST.index(kv[0]) if kv[0] in _ACTION_ALLOWLIST else len(_ACTION_ALLOWLIST), kv[0]))
    behavior = "· 행동: " + (" · ".join(f"{_ACTION_LABEL.get(a, a)} {n}" for a, n in ordered) or "없음")
    note = f" · ⚠️모델불일치 {s.model_mismatch_count}" if s.model_mismatch_count else ""
    # 비용 정직성: 실제 값이 0일 때만 '0원'. >0 이면 실제 USD + 경고(숨기지 않음).
    # provider 가 claude_cli_batch 가 아니면 구독이라 단정하지 않는다.
    mode = "구독" if s.provider == "claude_cli_batch" else f"provider={s.provider}"
    cost = s.direct_api_cost_usd
    cost_str = f"⚠️ 직접 API 비용 ${cost:g}" if cost and cost > 0 else "직접 API 비용 0원"
    model = f"· 모델: {_model_label(s.model_actual)} {mode} · {cost_str}{note}"
    queue_state = f"⚠️지연(최고 {s.oldest_due_age_min}분)" if s.oldest_due_age_min > 30 else "정상"
    queue = f"· 큐: {queue_state} · 다음 분석 {nxt:%H:%M}"
    return "\n".join([head, hostline, cand, slot, result, behavior, model, queue])


def send_vlm_run_summary(summary: VlmRunSummary, *, send_fn) -> bool:
    """Slack 1회 전송. 실패해도 예외를 흘리지 않고 False — VLM job 상태·Claude 재호출과 무관(§9)."""
    try:
        return bool(send_fn(format_vlm_run_summary(summary)))
    except Exception:  # noqa: BLE001 — Slack 실패는 VLM 결과를 바꾸지 않는다
        return False
