from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tests._fakes import FakeSB
from reporter.vlm_rolling import (
    CYCLE_CAP,
    DAILY_CAP,
    backfill_created_today,
    remaining_daily_budget,
    rolling_backfill_allowed_now,
)

_KST = ZoneInfo("Asia/Seoul")


def _kst(h, mi):
    return datetime(2026, 7, 16, h, mi, tzinfo=_KST)


def test_caps_are_30_and_600():
    assert CYCLE_CAP == 30 and DAILY_CAP == 600


def test_guard_skips_within_30min_of_regular_vlm():
    for h, mi in [(21, 35), (23, 35), (1, 35), (3, 35)]:  # 정규 :00 25분 전 → skip
        assert rolling_backfill_allowed_now(_kst(h, mi)) is False
    for h, mi in [(21, 45), (23, 45), (1, 45), (3, 45), (22, 0), (0, 0), (2, 0), (4, 0)]:
        assert rolling_backfill_allowed_now(_kst(h, mi)) is False  # 정규 근처/정각도 skip


def test_guard_allows_35min_after_regular_and_daytime():
    for h, mi in [(22, 35), (0, 35), (2, 35), (4, 35)]:  # 정규 35분 후 → 허용
        assert rolling_backfill_allowed_now(_kst(h, mi)) is True
    for h in (7, 10, 13, 16, 19):  # 정규와 먼 낮 :35
        assert rolling_backfill_allowed_now(_kst(h, 35)) is True


def test_guard_converts_utc_to_kst():
    # UTC 12:35 = KST 21:35 → skip
    assert rolling_backfill_allowed_now(datetime(2026, 7, 16, 12, 35, tzinfo=timezone.utc)) is False
    # UTC 13:35 = KST 22:35 → allow
    assert rolling_backfill_allowed_now(datetime(2026, 7, 16, 13, 35, tzinfo=timezone.utc)) is True


def _bf_job(created_at):
    return {"selector_version": "budget-router-backfill-20260707-14-v1", "created_at": created_at}


def test_daily_budget_counts_only_today_kst_backfill():
    # KST 2026-07-16 → UTC [07-15 15:00, 07-16 15:00). 오늘 5건 + 어제 3건 + regular 2건
    rows = [_bf_job("2026-07-16T01:00:00+00:00") for _ in range(5)]          # KST 10:00 오늘
    rows += [_bf_job("2026-07-15T10:00:00+00:00") for _ in range(3)]          # KST 19:00 어제
    rows += [{"selector_version": "budget-router-v1", "created_at": "2026-07-16T01:00:00+00:00"} for _ in range(2)]
    sb = FakeSB({"clip_vlm_jobs": rows})
    now = datetime(2026, 7, 16, 3, tzinfo=timezone.utc)  # KST 12:00
    assert backfill_created_today(sb, now) == 5  # regular·어제 제외
    assert remaining_daily_budget(sb, now) == DAILY_CAP - 5


def test_remaining_budget_zero_when_capped():
    rows = [_bf_job("2026-07-16T01:00:00+00:00") for _ in range(600)]
    sb = FakeSB({"clip_vlm_jobs": rows})
    now = datetime(2026, 7, 16, 3, tzinfo=timezone.utc)
    assert remaining_daily_budget(sb, now) == 0
