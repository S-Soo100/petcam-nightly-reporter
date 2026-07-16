"""worker._format — 움직임 수집 요약 Slack 카드(순수함수).

VLM 분석 결과와 혼동되지 않게 '움직임 수집 통계'임을 명시한다. SAMPLE_TOP_N=0 배포에서
legacy Claude 호출은 없다. 방어적으로 sampled>0 & 전량 실패인 경우에만 조용한 한도실패
경보를 유지(2026-07-07 교훈).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from reporter import config
from reporter.worker import _format

_KST = ZoneInfo("Asia/Seoul")
_NOW = datetime(2026, 7, 16, 2, 5, tzinfo=_KST)  # 02:05 실행 → 00:00~02:00 블록 표시


def _activity(clip_count=27):
    return {"clip_count": clip_count, "active_minutes": 14.5,
            "peak_hour_kst": 1, "hourly_kst": {1: 16, 0: 11}}


def _beh(sampled=0, failed=0):
    return {"sampled_count": sampled, "failed_infra": failed, "unseen": 0,
            "analyzed_ok": max(0, sampled - failed),
            "shed_observed": False, "drink_observed": False, "feeding_observed": False}


def test_format_movement_summary_shape(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_HOURS", 2.0)
    msg = _format(_activity(), _beh(), _NOW)
    assert "📊 움직임 수집 요약 (00:00~02:00)" in msg
    assert "· 감지 클립 27개 · 약 14.5분" in msg
    assert "· 집중 시간대: 1시" in msg
    assert "이 메시지는 움직임 수집 통계이며 VLM 분석 결과가 아님" in msg


def test_format_preserves_fractional_window_hours(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_HOURS", 0.5)
    msg = _format(_activity(), _beh(), _NOW)
    assert "📊 움직임 수집 요약 (01:30~02:00)" in msg


def test_format_sampling_off_has_no_behavior_or_alert():
    msg = _format(_activity(), _beh(sampled=0), _NOW)
    assert "특이행동" not in msg
    assert "⚠️" not in msg
    assert "샘플" not in msg  # SAMPLE_TOP_N=0 → 행동 라벨 라인 없음


def test_format_defensive_all_failed_alert_preserved():
    # 방어적: 누군가 SAMPLE_TOP_N 재활성 후 전량 실패면 조용한 한도실패 경보 유지
    msg = _format(_activity(), _beh(sampled=3, failed=3), _NOW)
    assert "샘플 VLM 분석 전량 실패 3/3" in msg


def test_format_no_clips_short_circuit():
    empty = {"clip_count": 0, "active_minutes": 0.0, "peak_hour_kst": None, "hourly_kst": {}}
    msg = _format(empty, _beh(), _NOW)
    assert "움직임 없음" in msg
