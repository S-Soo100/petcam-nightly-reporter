"""윈도우 시간 경계. motion_clips.started_at = SNTP UTC 라 UTC 로 계산."""
from datetime import datetime, timedelta, timezone


def window_bounds(now: datetime, hours: float) -> tuple[datetime, datetime]:
    """now(tz-aware) 기준 최근 `hours` 윈도우를 UTC [start, end) 로 반환.

    end = now(UTC), start = end - hours. motion_clips 는 started_at 이 UTC 라
    비교 전 UTC 로 맞춰야 경계가 정확하다(KST naive 로 비교하면 9시간 어긋남).
    """
    end = now.astimezone(timezone.utc)
    start = end - timedelta(hours=hours)
    return start, end
