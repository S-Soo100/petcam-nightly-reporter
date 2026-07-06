"""윈도우 활동 집계 — 순수 로직 TDD. W4a: claude 없이 motion_clips DB 필드만으로.

리포트 뼈대(활동량·활동시간대)는 motion_clips 자체가 모션 트리거라 DB 로 산출:
clip 존재 = 그 시각 활동. claude 는 W4b 에서 행동 종류 태깅에만 투입(샘플).
"""
from reporter.indexer import ClipMeta
from reporter.summarize import summarize_activity


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
