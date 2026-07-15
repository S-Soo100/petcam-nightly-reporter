"""historical VLM backfill 진행률 Slack 요약. BACKFILL_SELECTOR_VERSION 데이터만 집계하며
실제 backfill job 을 처리한 cycle 에서만 1회 전송한다(outside-hours/cooldown/no-op 은 로그만).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil
from zoneinfo import ZoneInfo

from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION, source_nights
from reporter.vlm_run_summary import friendly_host

KST = ZoneInfo("Asia/Seoul")
NIGHT_TARGET = 30
TOTAL_TARGET = NIGHT_TARGET * len(source_nights())  # 8박 × 30 = 240


@dataclass(frozen=True, slots=True)
class BackfillProgressSummary:
    host: str
    source_date: str
    this_run: dict          # {target, succeeded, retryable, failed, held}
    cumulative_succeeded: int
    total_target: int
    remaining: int
    next_run: datetime | None
    complete: bool = False
    completed_dates: int = 0
    total_dates: int = field(default=len(source_nights()))


def next_backfill_run(now: datetime) -> datetime:
    """다음 backfill 실행 시각(KST). 07~18시면 다음 정각, 그 외엔 다음 07:00."""
    kst = now.astimezone(KST).replace(minute=0, second=0, microsecond=0)
    if 7 <= kst.hour < 19:
        return kst + timedelta(hours=1)
    if kst.hour < 7:
        return kst.replace(hour=7)
    return (kst + timedelta(days=1)).replace(hour=7)


def _counts(stats) -> dict:
    counts = stats.counts if hasattr(stats, "counts") else (stats or {})
    return {
        "succeeded": counts.get("succeeded", 0),
        "retryable": counts.get("failed_retryable", 0),
        "failed": counts.get("failed_terminal", 0),
        "held": counts.get("held_model_mismatch", 0),
    }


def aggregate_backfill_progress(sb, *, source_date, host, now, this_run_stats, processed_target) -> BackfillProgressSummary:
    """backfill selector 전용 집계. 정규 selector 를 절대 포함하지 않는다."""
    rows = (sb.table("clip_vlm_jobs").select("status,rank_features")
            .eq("selector_version", BACKFILL_SELECTOR_VERSION).execute().data)
    cumulative = sum(1 for r in rows if r.get("status") == "succeeded")
    # 완료 판정: night 별 succeeded+failed_terminal>=30 인 date 수
    per_date: dict[str, dict] = {}
    for r in rows:
        d = (r.get("rank_features") or {}).get("source_date")
        if not d:
            continue
        bucket = per_date.setdefault(d, {"succeeded": 0, "terminal": 0})
        if r.get("status") == "succeeded":
            bucket["succeeded"] += 1
        elif r.get("status") == "failed_terminal":
            bucket["terminal"] += 1
    completed_dates = sum(1 for b in per_date.values() if b["succeeded"] + b["terminal"] >= NIGHT_TARGET)
    remaining = max(0, TOTAL_TARGET - cumulative)
    all_done = completed_dates >= len(source_nights())
    run = _counts(this_run_stats)
    run["target"] = processed_target
    return BackfillProgressSummary(
        host=host, source_date=str(source_date), this_run=run,
        cumulative_succeeded=cumulative, total_target=TOTAL_TARGET, remaining=remaining,
        next_run=None if all_done else next_backfill_run(now), complete=all_done,
        completed_dates=completed_dates,
    )


def _eta_hint(summary: BackfillProgressSummary) -> str:
    if summary.complete or summary.remaining <= 0:
        return "완료"
    nights = ceil(summary.remaining / NIGHT_TARGET)
    # 정밀 ETA 금지 — daytime(07~19시) 진행 기준 coarse 범위만.
    return f"남은 약 {nights}박 · daytime(07~19시) 진행 기준 수일 내"


def format_backfill_progress(summary: BackfillProgressSummary) -> str:
    host = friendly_host(summary.host)
    if summary.complete:
        return "\n".join([
            "📦 과거 영상 VLM 분석 — 전체 완료",
            f"· 실행 장비: {host}",
            f"· 누적: 완료 {summary.cumulative_succeeded} / 전체 {summary.total_target} · 날짜 {summary.completed_dates}/{summary.total_dates}",
            "· 남은 영상: 0 · 추가 실행 없음",
        ])
    run = summary.this_run
    lines = [
        "📦 과거 영상 VLM 분석",
        f"· 실행 장비: {host}",
        f"· 처리 날짜: {summary.source_date[5:].replace('-', '/')}",
        f"· 이번 실행: 대상 {run['target']} · 성공 {run['succeeded']} · 재시도 {run['retryable']} · 실패 {run['failed']}"
        + (f" · 모델보류 {run['held']}" if run['held'] else ""),
        f"· 누적: 완료 {summary.cumulative_succeeded} / 전체 {summary.total_target}",
        f"· 남은 영상: {summary.remaining}",
        f"· 예상 완료: {_eta_hint(summary)}",
        f"· 다음 실행: {summary.next_run.astimezone(KST):%m/%d %H:%M}" if summary.next_run else "· 다음 실행: 없음",
    ]
    return "\n".join(lines)


def send_backfill_progress(summary: BackfillProgressSummary, *, send_fn) -> bool:
    """Slack 1회. 실패해도 예외를 흘리지 않고 False — backfill 상태·Claude 재호출과 무관."""
    try:
        return bool(send_fn(format_backfill_progress(summary)))
    except Exception:  # noqa: BLE001
        return False
