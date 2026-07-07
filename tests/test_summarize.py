"""윈도우 활동 집계 — 순수 로직 TDD. W4a: claude 없이 motion_clips DB 필드만으로.

리포트 뼈대(활동량·활동시간대)는 motion_clips 자체가 모션 트리거라 DB 로 산출:
clip 존재 = 그 시각 활동. claude 는 W4b 에서 행동 종류 태깅에만 투입(샘플).
"""
from reporter.indexer import ClipMeta
from reporter.summarize import summarize_activity, summarize_behaviors


def _clip(started_at: str, duration_sec: float, motion: float = 0.5) -> ClipMeta:
    return ClipMeta(id="x", camera_id="c", started_at=started_at,
                    duration_sec=duration_sec, r2_key="k", motion_score=motion)


def test_summarize_activity():
    clips = [
        _clip("2026-07-06T13:00:00+00:00", 60),   # 22 KST
        _clip("2026-07-06T13:20:00+00:00", 40),   # 22 KST
        _clip("2026-07-06T15:00:00+00:00", 30),   # 00 KST (다음날)
    ]
    s = summarize_activity(clips)
    assert s["clip_count"] == 3
    assert s["active_minutes"] == 2.2            # (60+40+30)/60
    assert s["peak_hour_kst"] == 22              # 13 UTC = 22 KST, 2건 집중
    assert s["hourly_kst"][22] == 2
    assert s["hourly_kst"][0] == 1               # 15 UTC = 00 KST (다음날)


def test_summarize_activity_empty():
    s = summarize_activity([])
    assert s["clip_count"] == 0
    assert s["active_minutes"] == 0.0
    assert s["peak_hour_kst"] is None
    assert s["hourly_kst"] == {}


def test_summarize_behaviors():
    # classify_clip 결과들(샘플). 뼈대(activity)와 별개로 "무슨 행동이 관찰됐나".
    labeled = [
        {"action": "moving"},
        {"action": "moving"},
        {"action": "drinking"},
        {"action": "shedding"},
    ]
    b = summarize_behaviors(labeled)
    assert b["sampled_count"] == 4
    assert b["actions"] == {"moving": 2, "drinking": 1, "shedding": 1}
    assert b["shed_observed"] is True
    assert b["drink_observed"] is True
    assert b["feeding_observed"] is False   # eating_*/hand_feeding 없음
    assert b["failed_infra"] == 0
    assert b["analyzed_ok"] == 4


def test_summarize_behaviors_feeding_and_errors():
    labeled = [
        {"action": "eating_paste"},
        {"action": "hand_feeding"},
        {"action": "error"},                # 분류 실패는 행동 아님 — 카운트만
    ]
    b = summarize_behaviors(labeled)
    assert b["feeding_observed"] is True     # eating_paste 또는 hand_feeding
    assert b["shed_observed"] is False
    assert b["actions"]["error"] == 1
    assert b["failed_infra"] == 1
    assert b["analyzed_ok"] == 2


def test_summarize_behaviors_failed_infra_excludes_unseen():
    # failed_infra 는 claude 인프라 실패(error)만 — unseen(분석 도달·게코 부재/파싱실패)은 제외.
    labeled = [
        {"action": "error"},
        {"action": "error"},
        {"action": "unseen"},
        {"action": "moving"},
    ]
    b = summarize_behaviors(labeled)
    assert b["failed_infra"] == 2          # error 2건만
    assert b["analyzed_ok"] == 2           # 4 - 2 (unseen·moving 은 도달)


def test_summarize_behaviors_empty():
    b = summarize_behaviors([])
    assert b["sampled_count"] == 0
    assert b["actions"] == {}
    assert b["shed_observed"] is False
    assert b["drink_observed"] is False
    assert b["feeding_observed"] is False
    assert b["failed_infra"] == 0
    assert b["analyzed_ok"] == 0
