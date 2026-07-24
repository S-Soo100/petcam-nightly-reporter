"""짧은 영상 retention 모델·rounding 계약 테스트 (설계 §4).

DB 정본(petcam-lab 926e5f6)의 route/ fingerprint 계약을 로컬에서 동결한다. 네트워크·DB 무의존.
"""

from __future__ import annotations

import math

import pytest

from reporter.short_clip_retention_models import (
    DeletionClaim,
    DetectionResult,
    ShortClipCandidate,
    round_display_seconds,
)


# ── round_display_seconds: JavaScript Math.round(floor(x+0.5)), Python round() 아님 ──
def test_round_display_seconds_matches_js_round():
    assert round_display_seconds(3.5) == 4
    assert round_display_seconds(10.5) == 11  # Python round()면 10 (banker), floor(x+0.5)=11
    assert round_display_seconds(4.0) == 4
    assert round_display_seconds(4.49) == 4
    assert round_display_seconds(0.0) == 0


def test_round_display_seconds_rejects_invalid():
    for bad in (-0.1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            round_display_seconds(bad)


# ── ShortClipCandidate: strict parsing ──
def test_short_clip_candidate_from_row():
    c = ShortClipCandidate.from_row(
        {
            "clip_id": "11111111-1111-4111-8111-111111111111",
            "started_at": "2026-07-20T00:00:01+00:00",
            "duration_sec": 3.6,
            "displayed_duration_sec": 4,
            "current_state": "none",
        }
    )
    assert c.clip_id == "11111111-1111-4111-8111-111111111111"
    assert c.duration_sec == 3.6
    assert c.displayed_duration_sec == 4


def test_short_clip_candidate_rejects_missing_clip_id():
    with pytest.raises((KeyError, ValueError)):
        ShortClipCandidate.from_row({"started_at": "t", "duration_sec": 3.6})


# ── DetectionResult: route allowlist ──
def test_detection_result_allows_known_routes():
    for route in ("candidate", "quarantined", "protected", "reused", "reused_restored", "ineligible"):
        assert DetectionResult.from_row({"route": route}).route == route


def test_detection_result_rejects_unknown_route():
    with pytest.raises(ValueError):
        DetectionResult.from_row({"route": "auto_p0"})
    with pytest.raises(ValueError):
        DetectionResult.from_row({"route": None})


# ── DeletionClaim: UUID/token presence + lowercase sha256 + repr 비노출 ──
def _claim(**over):
    base = {
        "exclusion_id": "22222222-2222-4222-8222-222222222222",
        "clip_id": "33333333-3333-4333-8333-333333333333",
        "r2_key": "terra-clips/clips/exact.mp4",
        "lease_token": "44444444-4444-4444-8444-444444444444",
    }
    base.update(over)
    return DeletionClaim.from_row(base)


def test_deletion_claim_from_row_and_fingerprint():
    c = _claim()
    fp = c.key_fingerprint()
    assert len(fp) == 64
    assert fp == fp.lower()
    assert all(ch in "0123456789abcdef" for ch in fp)
    # 결정론: 같은 key → 같은 fingerprint.
    assert c.key_fingerprint() == _claim().key_fingerprint()


def test_deletion_claim_rejects_missing_fields():
    for missing in ("exclusion_id", "clip_id", "r2_key", "lease_token"):
        row = {
            "exclusion_id": "e",
            "clip_id": "c",
            "r2_key": "terra-clips/clips/x.mp4",
            "lease_token": "t",
        }
        del row[missing]
        with pytest.raises((KeyError, ValueError)):
            DeletionClaim.from_row(row)


def test_deletion_claim_repr_hides_key_and_token():
    text = repr(_claim())
    assert "terra-clips/clips/exact.mp4" not in text
    assert "44444444-4444-4444-8444-444444444444" not in text
    # exclusion_id 는 provenance 로 남아도 raw key/token 은 절대 repr 에 없다.
