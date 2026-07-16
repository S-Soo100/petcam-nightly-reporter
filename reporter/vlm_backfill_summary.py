"""historical VLM backfill 진행률 Slack 요약. BACKFILL_SELECTOR_VERSION 데이터만 집계하며
실제 backfill job 을 처리한 cycle 에서만 1회 전송한다(outside-hours/cooldown/no-op 은 로그만).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION
from reporter.vlm_run_summary import friendly_host

KST = ZoneInfo("Asia/Seoul")
NIGHT_TARGET = 30  # historical 참고(고정 240 프레이밍은 rolling 에서 제거)


@dataclass(frozen=True, slots=True)
class BackfillProgressSummary:
    host: str
    source_date: str
    this_run: dict          # {target, succeeded, retryable, failed, held}
    created: int            # 생성된 backfill job 수(누적)
    succeeded: int
    terminal: int           # failed_terminal — worker 가 COMPLETE 로 취급, 재처리 안 함
    in_progress: int        # queued + failed_retryable + held_model_mismatch (+processing)
    waiting_dates: int      # 아직 처리되지 않은 closed source nights 수(rolling backlog)
    next_run: datetime | None
    total_target: int = 0   # rolling 에선 고정 목표 없음(historical 호환 필드)
    complete: bool = False

    @property
    def processed(self) -> int:
        """worker 의 COMPLETE_STATUSES 와 동일: succeeded + failed_terminal."""
        return self.succeeded + self.terminal


def next_backfill_run(now: datetime) -> datetime:
    """다음 rolling 실행 시각(KST) = 다음 매시간 :35."""
    kst = now.astimezone(KST)
    candidate = kst.replace(minute=35, second=0, microsecond=0)
    if kst >= candidate:
        candidate += timedelta(hours=1)
    return candidate


def _counts(stats) -> dict:
    counts = stats.counts if hasattr(stats, "counts") else (stats or {})
    return {
        "succeeded": counts.get("succeeded", 0),
        "retryable": counts.get("failed_retryable", 0),
        "failed": counts.get("failed_terminal", 0),
        "held": counts.get("held_model_mismatch", 0),
    }


def aggregate_backfill_progress(sb, *, source_date, host, now, this_run_stats, processed_target, waiting_dates=0) -> BackfillProgressSummary:
    """rolling backfill selector 전용 집계. 정규 selector 를 절대 포함하지 않는다."""
    rows = (sb.table("clip_vlm_jobs").select("status")
            .eq("selector_version", BACKFILL_SELECTOR_VERSION).execute().data)
    created = len(rows)
    succeeded = sum(1 for r in rows if r.get("status") == "succeeded")
    terminal = sum(1 for r in rows if r.get("status") == "failed_terminal")
    in_progress = sum(1 for r in rows if r.get("status") in ("queued", "failed_retryable", "submitted", "processing", "held_model_mismatch"))
    run = _counts(this_run_stats)
    run["target"] = processed_target
    return BackfillProgressSummary(
        host=host, source_date=str(source_date), this_run=run,
        created=created, succeeded=succeeded, terminal=terminal, in_progress=in_progress,
        waiting_dates=waiting_dates, next_run=next_backfill_run(now),
    )


def format_backfill_progress(summary: BackfillProgressSummary) -> str:
    host = friendly_host(summary.host)
    run = summary.this_run
    lines = [
        "📦 과거 영상 VLM 분석",
        f"· 실행 장비: {host}",
        f"· 처리 날짜: {summary.source_date[5:].replace('-', '/')}",
        f"· 이번 실행: 대상 {run['target']} · 성공 {run['succeeded']} · 재시도 {run['retryable']} · 영구실패 {run['failed']}"
        + (f" · 모델보류 {run['held']}" if run['held'] else ""),
        f"· 누적 처리: {summary.processed} (성공 {summary.succeeded} · 영구실패 {summary.terminal})",
        f"· 현재 backlog: 진행 중 {summary.in_progress} · 대기 날짜 {summary.waiting_dates}일",
        f"· 다음 실행: {summary.next_run.astimezone(KST):%m/%d %H:%M}" if summary.next_run else "· 다음 실행: 없음",
        "· 정규 VLM 보호: 정상",
    ]
    return "\n".join(lines)


def send_backfill_progress(summary: BackfillProgressSummary, *, send_fn) -> bool:
    """Slack 1회. 실패해도 예외를 흘리지 않고 False — backfill 상태·Claude 재호출과 무관."""
    try:
        return bool(send_fn(format_backfill_progress(summary)))
    except Exception:  # noqa: BLE001
        return False
