from datetime import date, datetime
from zoneinfo import ZoneInfo

from reporter.vlm_backfill_selector import (
    EPOCH_START,
    bucket_plans,
    latest_closed_source_date,
    rolling_source_nights,
)

_KST = ZoneInfo("Asia/Seoul")


def _kst(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=_KST)


def test_epoch_start_is_2026_07_07():
    assert EPOCH_START == date(2026, 7, 7)


def test_latest_closed_only_returns_fully_ended_nights():
    # 07-16 09:46 → 어젯밤(07-15, 07-16 06:00 종료)이 닫힘
    assert latest_closed_source_date(_kst(2026, 7, 16, 9, 46)) == date(2026, 7, 15)
    # 06:00 정각 → 07-15 닫힘(<=)
    assert latest_closed_source_date(_kst(2026, 7, 16, 6, 0)) == date(2026, 7, 15)


def test_00_to_05_excludes_in_progress_night():
    # 07-16 03:00 → 07-15 밤은 07-16 06:00까지 진행 중 → 아직 안 닫힘 → 07-14
    assert latest_closed_source_date(_kst(2026, 7, 16, 3, 0)) == date(2026, 7, 14)
    assert latest_closed_source_date(_kst(2026, 7, 16, 5, 59)) == date(2026, 7, 14)
    # 20:30 저녁(오늘밤 진행 중) → 어젯밤 닫힘
    assert latest_closed_source_date(_kst(2026, 7, 16, 20, 30)) == date(2026, 7, 15)


def test_kst_month_and_year_boundaries():
    assert latest_closed_source_date(_kst(2026, 8, 1, 9, 0)) == date(2026, 7, 31)
    assert latest_closed_source_date(_kst(2027, 1, 1, 9, 0)) == date(2026, 12, 31)
    assert latest_closed_source_date(_kst(2027, 1, 1, 3, 0)) == date(2026, 12, 30)  # 진행중 밤 제외


def test_utc_input_converted_to_kst_not_misread():
    # UTC 2026-07-15 22:00 = KST 2026-07-16 07:00 → 07-15 닫힘
    assert latest_closed_source_date(datetime(2026, 7, 15, 22, tzinfo=ZoneInfo("UTC"))) == date(2026, 7, 15)


def test_rolling_source_nights_grows_with_new_closed_nights():
    assert rolling_source_nights(EPOCH_START, _kst(2026, 7, 16, 9, 46)) == tuple(
        date(2026, 7, d) for d in range(7, 16))  # 07-07..07-15 (9박)
    # 하루 뒤 → 07-16 추가
    assert rolling_source_nights(EPOCH_START, _kst(2026, 7, 17, 9, 0))[-1] == date(2026, 7, 16)
    # 아직 closed night 없음
    assert rolling_source_nights(date(2026, 7, 20), _kst(2026, 7, 16, 9, 0)) == ()


def test_bucket_plans_generalizes_to_arbitrary_dates_stable_index():
    # 기존 고정 날짜는 index 불변(기존 job window_start 보존)
    for i, d in enumerate(date(2026, 7, day) for day in range(7, 15)):
        plans = bucket_plans(d)
        assert len(plans) == 8
        assert plans[0].start.astimezone(_KST).hour == 20  # night 20:00 시작
    # 신규 날짜(07-15 = EPOCH+8, index 0)도 8 bucket 생성
    new_plans = bucket_plans(date(2026, 7, 15))
    assert len(new_plans) == 8
    assert new_plans[0].start.astimezone(_KST).hour == 20
