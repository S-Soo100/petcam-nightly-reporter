"""activity_indexer.list_unprocessed_clips — allowlist 카메라의 미처리 clip 선택.

미처리 = clip_activity_assessments 에 (현재 policy_version) 이 **없는** clip. assessment 기준이라:
- prelabel 만 있고 assessment 저장 실패한 clip 도 재처리(self-healing, 영구 pending 방지 = 하드닝 4)
- policy_version 이 바뀌면 자동으로 재평가 대상(하드닝 3, worker 가 기존 prelabel 재사용)
motion_clips 는 불변(상태 컬럼 금지). pagination 으로 오래된 처리분 prefix starvation 방지(하드닝 1).
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
    out = list_unprocessed_clips(sb, ["A", "B"], "pol-v0", T0, T3)
    assert {c.id for c in out} == {"c1", "c2", "c3"}  # c4(camera C) 제외


def test_excludes_clips_with_assessment_same_policy():
    sb = FakeSB({"motion_clips": MC, "clip_activity_assessments": [
        {"clip_id": "c1", "policy_version": "pol-v0"},
    ]})
    out = list_unprocessed_clips(sb, ["A", "B"], "pol-v0", T0, T3)
    assert {c.id for c in out} == {"c2", "c3"}


def test_different_policy_version_counts_as_unprocessed():
    sb = FakeSB({"motion_clips": MC, "clip_activity_assessments": [
        {"clip_id": "c1", "policy_version": "pol-OLD"},  # 다른 정책
    ]})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3)
    assert "c1" in {c.id for c in out}


def test_prelabel_without_assessment_is_reprocessed():
    # 하드닝 4: prelabel 은 저장됐지만 assessment 저장이 실패해 남은 clip → 재처리(영구 pending 방지)
    sb = FakeSB({
        "motion_clips": [MC[0]],
        "clip_prelabels": [{"clip_id": "c1", "model_version": "gv", "schema_version": "sv"}],
        "clip_activity_assessments": [],  # assessment 없음
    })
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3)
    assert "c1" in {c.id for c in out}


def test_empty_allowlist_returns_empty():
    sb = FakeSB({"motion_clips": MC})
    assert list_unprocessed_clips(sb, [], "pol-v0", T0, T3) == []


def test_window_excludes_out_of_range():
    sb = FakeSB({"motion_clips": MC})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", _dt("2026-07-14T01:30:00+00:00"), T3)
    assert {c.id for c in out} == {"c2"}


def test_returns_clipmeta_with_fields():
    sb = FakeSB({"motion_clips": MC})
    out = list_unprocessed_clips(sb, ["B"], "pol-v0", T0, T3)
    assert len(out) == 1
    assert out[0].r2_key == "k3"
    assert out[0].duration_sec == 10.0


def test_no_starvation_when_processed_prefix_fills_limit():
    # 오래된 c1~c3 가 assessment(pol-v0) 로 처리됨. limit=2 page_size=2 → 첫 페이지(c1,c2) 전부 done.
    # 버그면 [] 반환(최신 미처리 c4,c5 starvation), 수정(pagination)이면 c4,c5.
    mc5 = [{"id": f"c{i}", "camera_id": "A", "started_at": f"2026-07-14T0{i}:00:00+00:00",
            "duration_sec": 10.0, "r2_key": f"k{i}", "motion_score": 0.1} for i in range(1, 6)]
    sb = FakeSB({"motion_clips": mc5, "clip_activity_assessments": [
        {"clip_id": "c1", "policy_version": "pol-v0"},
        {"clip_id": "c2", "policy_version": "pol-v0"},
        {"clip_id": "c3", "policy_version": "pol-v0"},
    ]})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0",
                                 _dt("2026-07-14T00:00:00+00:00"), _dt("2026-07-14T09:00:00+00:00"),
                                 limit=2, page_size=2)
    assert {c.id for c in out} == {"c4", "c5"}


def test_pagination_stops_at_limit():
    mc5 = [{"id": f"c{i}", "camera_id": "A", "started_at": f"2026-07-14T0{i}:00:00+00:00",
            "duration_sec": 10.0, "r2_key": f"k{i}", "motion_score": 0.1} for i in range(1, 6)]
    sb = FakeSB({"motion_clips": mc5})  # 전부 미처리
    out = list_unprocessed_clips(sb, ["A"], "pol-v0",
                                 _dt("2026-07-14T00:00:00+00:00"), _dt("2026-07-14T09:00:00+00:00"),
                                 limit=3, page_size=2)
    assert len(out) == 3
    assert [c.id for c in out] == ["c1", "c2", "c3"]
