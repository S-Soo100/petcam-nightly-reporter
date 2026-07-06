"""윈도우 시간 경계 계산 — 순수 로직 TDD."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from reporter.timewin import window_bounds


def test_window_bounds_2h():
    # 10:30 KST = 01:30 UTC (KST = UTC+9)
    now = datetime(2026, 7, 6, 10, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    start, end = window_bounds(now, hours=2)
    assert end == datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc)
    assert start == datetime(2026, 7, 5, 23, 30, tzinfo=timezone.utc)


def test_window_bounds_naive_utc_output():
    # 반환은 항상 UTC tz-aware (motion_clips.started_at 이 UTC 라 비교 안전)
    now = datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)
    start, end = window_bounds(now, hours=1.5)
    assert end.tzinfo == timezone.utc
    assert start == datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc)
