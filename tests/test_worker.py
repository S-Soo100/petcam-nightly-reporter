"""worker._format — Slack 카드 포매팅(순수함수). claude 실패 경보 회귀 방어(2026-07-07).

'조용한 한도실패'(claude 전량 error 인데 리포트는 '특이행동 없음' 으로 정상처럼 나감)를
막는 게 이 포맷 로직의 핵심. 그 경보 분기를 고정한다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from reporter.worker import _format

_KST = ZoneInfo("Asia/Seoul")
_NOW = datetime(2026, 7, 7, 3, 0, tzinfo=_KST)


def _activity(clip_count=10):
    return {"clip_count": clip_count, "active_minutes": 5.0,
            "peak_hour_kst": 2, "hourly_kst": {2: 6, 3: 4}}


def _beh(sampled, failed, shed=False, drink=False, feed=False, unseen=0):
    return {"sampled_count": sampled, "failed_infra": failed, "unseen": unseen,
            "analyzed_ok": sampled - failed,
            "shed_observed": shed, "drink_observed": drink, "feeding_observed": feed}


def test_format_all_failed_shows_alert_not_silent():
    # claude 전량 실패 → '특이행동 없음'(정상처럼) 이 아니라 경보로 (조용한 실패 방지)
    msg = _format(_activity(), _beh(5, 5), _NOW)
    assert "분석실패 5/5" in msg
    assert "특이행동 없음" not in msg


def test_format_partial_failed_tags_alert():
    msg = _format(_activity(), _beh(5, 2, shed=True), _NOW)
    assert "🧬탈피" in msg
    assert "일부실패 2/5" in msg


def test_format_clean_no_alert():
    msg = _format(_activity(), _beh(5, 0, drink=True), _NOW)
    assert "💧음수" in msg
    assert "⚠️" not in msg


def test_format_unseen_shows_gecko_absent():
    # 분석한 clip 이 unseen → '특이행동 없음' 이 아니라 '게코 안 보임(분석불필요)' 로 구분
    msg = _format(_activity(), _beh(1, 0, unseen=1), _NOW)
    assert "게코 안 보임" in msg
    assert "특이행동 없음" not in msg


def test_format_no_behavior_but_success_no_alert():
    # 성공했는데 특이행동만 없음 → 경보 없이 '특이행동 없음'
    msg = _format(_activity(), _beh(5, 0), _NOW)
    assert "특이행동 없음" in msg
    assert "⚠️" not in msg


def test_format_no_clips_short_circuit():
    empty = {"clip_count": 0, "active_minutes": 0.0, "peak_hour_kst": None, "hourly_kst": {}}
    msg = _format(empty, _beh(0, 0), _NOW)
    assert "활동 없음" in msg
