"""Rolling backfill 스케줄 guard + durable 일일 처리량. 정규 VLM(22/00/02/04) 우선.

schedule guard 는 lock/DB/R2/Gate/Claude 전에 실행돼야 한다(fail-closed). 일일 상한은
프로세스 메모리가 아니라 production clip_vlm_jobs.created_at(KST 오늘) 로 durable 계산한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION

KST = ZoneInfo("Asia/Seoul")
CYCLE_CAP = 30
DAILY_CAP = 600
_REGULAR_MINUTES = (0, 120, 240, 1320)  # 00:00, 02:00, 04:00, 22:00 KST (정규 VLM)
_GUARD_MIN = 30


def rolling_backfill_allowed_now(now: datetime) -> bool:
    """정규 VLM ±30분이면 False(no-op). 24시간 매시간 :35 실행 중 :35=35분 후만 허용."""
    kst = now.astimezone(KST)
    minute_of_day = kst.hour * 60 + kst.minute
    for regular in _REGULAR_MINUTES:
        dist = abs(minute_of_day - regular)
        dist = min(dist, 1440 - dist)  # 자정 wrap 원형 거리
        if dist <= _GUARD_MIN:
            return False
    return True


def next_regular_vlm(now: datetime) -> datetime:
    """다음 정규 VLM 시각(22/00/02/04 KST)을 UTC 로 반환. backfill runtime deadline 계산용."""
    kst = now.astimezone(KST)
    candidates = []
    for day_offset in (0, 1):
        day = (kst + timedelta(days=day_offset)).date()
        for hour in (0, 2, 4, 22):
            t = datetime(day.year, day.month, day.day, hour, tzinfo=KST)
            if t > kst:
                candidates.append(t)
    return min(candidates).astimezone(timezone.utc)


def _kst_day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    kst = now.astimezone(KST)
    start = datetime(kst.year, kst.month, kst.day, tzinfo=KST)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def backfill_created_today(sb, now: datetime) -> int:
    """KST 오늘 생성된 backfill selector job 수(durable). 정규 selector·다른 날 제외."""
    start, end = _kst_day_bounds_utc(now)
    rows = (sb.table("clip_vlm_jobs").select("created_at")
            .eq("selector_version", BACKFILL_SELECTOR_VERSION)
            .gte("created_at", start.isoformat()).lt("created_at", end.isoformat())
            .execute().data)
    return len(rows)


def remaining_daily_budget(sb, now: datetime) -> int:
    return max(0, DAILY_CAP - backfill_created_today(sb, now))
