"""activity_indexer.list_unprocessed_clips — allowlist 카메라의 미처리 clip 선택.

미처리 = clip_prelabels 에 (같은 model_version+schema_version) 이 없는 clip.
motion_clips 에 상태 컬럼을 안 붙이고(지시문 §438) evidence 존재 여부로 판별 → 멱등·불변.
"""

from datetime import datetime

from _fakes import FakeSB

from reporter.activity_indexer import list_unprocessed_clips


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


MC = [
    {"id": "c1", "camera_id": "A", "started_at": "2026-07-14T01:00:00+00:00",
     "duration_sec": 30.0, "r2_key": "k1", "motion_score": 0.1},
    {"id": "c2", "camera_id": "A", "started_at": "2026-07-14T02:00:00+00:00",
     "duration_sec": 20.0, "r2_key": "k2", "motion_score": 0.2},
    {"id": "c3", "camera_id": "B", "started_at": "2026-07-14T02:30:00+00:00",
     "duration_sec": 10.0, "r2_key": "k3", "motion_score": 0.3},
    {"id": "c4", "camera_id": "C", "started_at": "2026-07-14T02:00:00+00:00",
     "duration_sec": 10.0, "r2_key": "k4", "motion_score": 0.3},  # allowlist 밖
]
T0 = _dt("2026-07-14T00:00:00+00:00")
T3 = _dt("2026-07-14T03:00:00+00:00")


def test_only_allowlist_cameras_within_window():
    sb = FakeSB({"motion_clips": MC})
    out = list_unprocessed_clips(sb, ["A", "B"], "gv2", "sv1", T0, T3)
    assert {c.id for c in out} == {"c1", "c2", "c3"}  # c4(camera C) 제외


def test_excludes_already_processed_same_version():
    sb = FakeSB({"motion_clips": MC, "clip_prelabels": [
        {"clip_id": "c1", "model_version": "gv2", "schema_version": "sv1"},
    ]})
    out = list_unprocessed_clips(sb, ["A", "B"], "gv2", "sv1", T0, T3)
    assert {c.id for c in out} == {"c2", "c3"}


def test_different_model_version_counts_as_unprocessed():
    sb = FakeSB({"motion_clips": MC, "clip_prelabels": [
        {"clip_id": "c1", "model_version": "gv1", "schema_version": "sv1"},  # 다른 버전
    ]})
    out = list_unprocessed_clips(sb, ["A"], "gv2", "sv1", T0, T3)
    assert "c1" in {c.id for c in out}


def test_empty_allowlist_returns_empty():
    sb = FakeSB({"motion_clips": MC})
    assert list_unprocessed_clips(sb, [], "gv2", "sv1", T0, T3) == []


def test_window_excludes_out_of_range():
    sb = FakeSB({"motion_clips": MC})
    out = list_unprocessed_clips(sb, ["A"], "gv2", "sv1", _dt("2026-07-14T01:30:00+00:00"), T3)
    assert {c.id for c in out} == {"c2"}  # c1(01:00) 은 [01:30,03:00) 밖


def test_returns_clipmeta_with_fields():
    sb = FakeSB({"motion_clips": MC})
    out = list_unprocessed_clips(sb, ["B"], "gv2", "sv1", T0, T3)
    assert len(out) == 1
    assert out[0].r2_key == "k3"
    assert out[0].duration_sec == 10.0
