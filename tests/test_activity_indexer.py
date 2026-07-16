"""activity_indexer.list_unprocessed_clips — allowlist 카메라의 미처리 clip 선택.

미처리 = clip_activity_assessments 에 (현재 policy_version) 이 없거나, 있어도 그 assessment 가
가리키는 prelabel 의 frames_sampled 가 min_frames 미만인 clip (self-healing, 설계 §5). 즉:
- prelabel 만 있고 assessment 저장 실패한 clip → 재처리(영구 pending 방지 = 하드닝 4)
- policy_version 이 바뀌면 자동 재평가(하드닝 3)
- assessment 는 있지만 참조 prelabel 이 없거나 frames_sampled<min → 불완전 → 재선정(self-healing)
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


def _done(clip_id, policy="pol-v0", pid=None, frames=12):
    """clip 을 '완료'로 만드는 assessment + 완전 prelabel 링크 쌍을 반환."""
    pid = pid or f"pre-{clip_id}"
    return (
        {"clip_id": clip_id, "policy_version": policy, "prelabel_id": pid},
        {"id": pid, "clip_id": clip_id, "frames_sampled": frames},
    )


def test_only_allowlist_cameras_within_window():
    sb = FakeSB({"motion_clips": MC})
    out = list_unprocessed_clips(sb, ["A", "B"], "pol-v0", T0, T3)
    assert {c.id for c in out} == {"c1", "c2", "c3"}  # c4(camera C) 제외


def test_excludes_clips_with_complete_assessment():
    a, p = _done("c1")
    sb = FakeSB({"motion_clips": MC, "clip_activity_assessments": [a], "clip_prelabels": [p]})
    out = list_unprocessed_clips(sb, ["A", "B"], "pol-v0", T0, T3)
    assert {c.id for c in out} == {"c2", "c3"}  # c1 완료(12프레임) → 제외


def test_different_policy_version_counts_as_unprocessed():
    a, p = _done("c1", policy="pol-OLD")  # 완전하지만 다른 정책
    sb = FakeSB({"motion_clips": MC, "clip_activity_assessments": [a], "clip_prelabels": [p]})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3)
    assert "c1" in {c.id for c in out}


def test_prelabel_without_assessment_is_reprocessed():
    # 하드닝 4: prelabel 은 저장됐지만 assessment 저장이 실패해 남은 clip → 재처리(영구 pending 방지)
    sb = FakeSB({
        "motion_clips": [MC[0]],
        "clip_prelabels": [{"id": "pre-c1", "clip_id": "c1", "frames_sampled": 12}],
        "clip_activity_assessments": [],  # assessment 없음
    })
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3)
    assert "c1" in {c.id for c in out}


# --- self-healing: 불완전 assessment 재선정 (설계 §5) ---

def test_incomplete_assessment_zero_frames_is_requeued():
    # assessment 는 있지만 참조 prelabel frames_sampled=0 → 불완전 → 재선정
    a, p = _done("c1", frames=0)
    sb = FakeSB({"motion_clips": MC, "clip_activity_assessments": [a], "clip_prelabels": [p]})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3)
    assert "c1" in {c.id for c in out}


def test_incomplete_assessment_five_frames_is_requeued():
    a, p = _done("c1", frames=5)  # 5 < min 6
    sb = FakeSB({"motion_clips": MC, "clip_activity_assessments": [a], "clip_prelabels": [p]})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3)
    assert "c1" in {c.id for c in out}


def test_complete_assessment_six_frames_is_done():
    a, p = _done("c1", frames=6)  # 정확히 min 6 → 완료
    sb = FakeSB({"motion_clips": MC, "clip_activity_assessments": [a], "clip_prelabels": [p]})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3)
    assert "c1" not in {c.id for c in out}


def test_assessment_missing_referenced_prelabel_is_requeued():
    # assessment 는 prelabel_id 를 가리키지만 그 prelabel row 가 없음 → 불완전 → 재선정
    sb = FakeSB({"motion_clips": MC,
                 "clip_activity_assessments": [{"clip_id": "c1", "policy_version": "pol-v0",
                                                "prelabel_id": "ghost"}],
                 "clip_prelabels": []})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3)
    assert "c1" in {c.id for c in out}


def test_min_frames_argument_is_respected():
    # min_frames=3 이면 frames_sampled=5 도 완료로 인정 (정책 최소치가 인자로 흐름을 검증)
    a, p = _done("c1", frames=5)
    sb = FakeSB({"motion_clips": MC, "clip_activity_assessments": [a], "clip_prelabels": [p]})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0", T0, T3, min_frames=3)
    assert "c1" not in {c.id for c in out}


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
    # 오래된 c1~c3 가 완전 evidence 로 처리됨. limit=2 page_size=2 → 첫 페이지(c1,c2) 전부 done.
    # 버그면 [] 반환(최신 미처리 c4,c5 starvation), 수정(pagination)이면 c4,c5.
    mc5 = [{"id": f"c{i}", "camera_id": "A", "started_at": f"2026-07-14T0{i}:00:00+00:00",
            "duration_sec": 10.0, "r2_key": f"k{i}", "motion_score": 0.1} for i in range(1, 6)]
    assess, pre = [], []
    for cid in ("c1", "c2", "c3"):
        a, p = _done(cid)
        assess.append(a); pre.append(p)
    sb = FakeSB({"motion_clips": mc5, "clip_activity_assessments": assess, "clip_prelabels": pre})
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


def test_mixed_pages_over_1000_clips():
    # 1200 clip: 짝수 index 는 완전(12), 홀수 index 는 불완전(0). 페이지 경계를 넘어서도
    # 완전한 것만 done, 불완전은 재선정. limit 크게 두고 전수 확인.
    n = 1200
    mc = [{"id": f"c{i:04d}", "camera_id": "A",
           "started_at": f"2026-07-14T00:00:{i%60:02d}+00:00" if i < 60 else "2026-07-14T01:00:00+00:00",
           "duration_sec": 10.0, "r2_key": f"k{i}", "motion_score": 0.1} for i in range(n)]
    # started_at 을 단조 증가시켜 정렬 안정화
    for i, r in enumerate(mc):
        r["started_at"] = f"2026-07-{14 + i // 500:02d}T{(i % 24):02d}:{(i % 60):02d}:00+00:00"
    assess, pre = [], []
    for i in range(n):
        if i % 2 == 0:  # 완전
            a, p = _done(f"c{i:04d}", frames=12)
        else:  # 불완전
            a, p = _done(f"c{i:04d}", frames=0)
        assess.append(a); pre.append(p)
    sb = FakeSB({"motion_clips": mc, "clip_activity_assessments": assess, "clip_prelabels": pre})
    out = list_unprocessed_clips(sb, ["A"], "pol-v0",
                                 _dt("2026-06-01T00:00:00+00:00"), _dt("2026-08-01T00:00:00+00:00"),
                                 limit=2000, page_size=500)
    got = {c.id for c in out}
    # 홀수(불완전) 600 개 전부 재선정, 짝수(완전) 0 개
    assert len(got) == 600
    assert all(int(cid[1:]) % 2 == 1 for cid in got)
