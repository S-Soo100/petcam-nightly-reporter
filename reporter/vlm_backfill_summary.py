"""historical VLM backfill 진행률 Slack 요약. BACKFILL_SELECTOR_VERSION 데이터만 집계하며
실제 backfill job 을 처리한 cycle 에서만 1회 전송한다(outside-hours/cooldown/no-op 은 로그만).
"""

from __future__ import annotations

from dataclasses import dataclass
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
    created: int            # 생성된 backfill job 수
    succeeded: int
    terminal: int           # failed_terminal — worker 가 COMPLETE 로 취급, 재처리 안 함
    in_progress: int        # queued + failed_retryable + held_model_mismatch (+processing)
    total_target: int
    next_run: datetime | None
    complete: bool = False

    @property
    def processed(self) -> int:
        """worker 의 COMPLETE_STATUSES 와 동일: succeeded + failed_terminal."""
        return self.succeeded + self.terminal

    @property
    def not_created(self) -> int:
        return max(0, self.total_target - self.created)

    @property
    def remaining_processing(self) -> int:
        """남은 처리 = 목표 − 처리. failed_terminal 은 처리 완료라 여기 포함 안 됨."""
        return max(0, self.total_target - self.processed)


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
    rows = (sb.table("clip_vlm_jobs").select("status")
            .eq("selector_version", BACKFILL_SELECTOR_VERSION).execute().data)
    created = len(rows)
    succeeded = sum(1 for r in rows if r.get("status") == "succeeded")
    terminal = sum(1 for r in rows if r.get("status") == "failed_terminal")
    in_progress = sum(1 for r in rows if r.get("status") in ("queued", "failed_retryable", "processing", "held_model_mismatch"))
    processed = succeeded + terminal
    all_done = processed >= TOTAL_TARGET
    run = _counts(this_run_stats)
    run["target"] = processed_target
    return BackfillProgressSummary(
        host=host, source_date=str(source_date), this_run=run,
        created=created, succeeded=succeeded, terminal=terminal, in_progress=in_progress,
        total_target=TOTAL_TARGET, next_run=None if all_done else next_backfill_run(now), complete=all_done,
    )


def _eta_hint(summary: BackfillProgressSummary) -> str:
    # ETA 는 succeeded 가 아니라 '남은 처리량'(목표−처리) 기준. 정밀 ETA 금지.
    if summary.complete or summary.remaining_processing <= 0:
        return "완료"
    nights = ceil(summary.remaining_processing / NIGHT_TARGET)
    return f"남은 처리 약 {nights}박 · daytime(07~19시) 진행 기준 수일 내"


def format_backfill_progress(summary: BackfillProgressSummary) -> str:
    host = friendly_host(summary.host)
    if summary.complete:
        return "\n".join([
            "📦 과거 영상 VLM 분석 — 전체 완료",
            f"· 실행 장비: {host}",
            f"· 처리: {summary.processed}/{summary.total_target} (성공 {summary.succeeded} · 영구실패 {summary.terminal})",
            "· 남은 처리: 0 · 추가 실행 없음",
        ])
    run = summary.this_run
    lines = [
        "📦 과거 영상 VLM 분석",
        f"· 실행 장비: {host}",
        f"· 처리 날짜: {summary.source_date[5:].replace('-', '/')}",
        f"· 이번 실행: 대상 {run['target']} · 성공 {run['succeeded']} · 재시도 {run['retryable']} · 실패 {run['failed']}"
        + (f" · 모델보류 {run['held']}" if run['held'] else ""),
        f"· 누적 처리: {summary.processed}/{summary.total_target} (성공 {summary.succeeded} · 영구실패 {summary.terminal})",
        f"· 진행 중: {summary.in_progress} · 미생성: {summary.not_created}",
        f"· 남은 처리: {summary.remaining_processing}",
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
