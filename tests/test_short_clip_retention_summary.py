"""짧은 영상 retention 일일 Slack 카드 formatter 테스트 (설계 §8).

exact 한국어 필드 + 안전 라벨만. raw key/URL/UUID/token/fingerprint/DB message 절대 없음.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from reporter.short_clip_retention_summary import format_short_clip_retention_summary

NOW_KST = datetime(2026, 7, 25, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))


def test_summary_has_exact_korean_fields():
    stats = {
        "candidate": 34,
        "quarantined": 31,
        "review_pending": 3,
        "restored": 0,
        "pending_delete": 31,
        "deleted": 12,
        "blocked": 1,
        "device_label": "Mac mini",
        "rule_version": "short-device-error-v1",
    }
    text = format_short_clip_retention_summary(stats, NOW_KST)
    assert text.startswith("🗑️ 짧은 영상 장치 오류")
    assert "· 후보 34 · 자동 제외 31 · 검수 대기 3" in text
    assert "· Owner 복구 0 · 7일 후 삭제 예정 31" in text
    assert "· 오늘 R2 삭제 12 · 삭제 차단 1" in text
    assert "· 실행 장비: Mac mini · 규칙 short-device-error-v1" in text


def test_summary_defaults_missing_counts_to_zero():
    text = format_short_clip_retention_summary({}, NOW_KST)
    assert "· 후보 0 · 자동 제외 0 · 검수 대기 0" in text
    assert "· 오늘 R2 삭제 0 · 삭제 차단 0" in text
    # 라벨 기본값.
    assert "실행 장비: Mac mini" in text
    assert "규칙 short-device-error-v1" in text


def test_summary_never_leaks_raw_fields():
    # 카드는 count/안전 라벨만 — key/url/uuid/token/fingerprint 를 넣어도 렌더에 새지 않는다.
    stats = {
        "candidate": 1,
        "r2_key": "terra-clips/clips/secret.mp4",
        "lease_token": "44444444-4444-4444-8444-444444444444",
        "fingerprint": "a" * 64,
        "endpoint": "https://acct.r2.cloudflarestorage.com",
    }
    text = format_short_clip_retention_summary(stats, NOW_KST)
    for leak in ("terra-clips", "44444444", "a" * 64, "cloudflarestorage", "https://"):
        assert leak not in text
