"""activity_worker — batch 오케스트레이션(오류 격리·decision 집계·멱등 저장) + run 게이트.

process_batch 는 download/assess/store 를 주입받는 순수 오케스트레이션 → 실 R2/모델 없이 검증.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from _fakes import FakeSB

from gecko_vision_gate.activity_policy import ActivityAssessment, ActivityPolicy
from gecko_vision_gate.motion_evidence import MotionMetrics
from gecko_vision_gate.provenance import SAMPLER_VERSION, SCHEMA_VERSION, GateProvenance
from gecko_vision_gate.schema import PrelabelResult

from reporter import activity_worker
from reporter.activity_settings import CameraFilterSetting
from reporter.activity_store import (
    ProducerInfo,
    find_prelabel,
    reconstruct_evidence,
    store_assessment_only,
    store_evidence_and_assessment,
)
from reporter.gate_runner import GateAssessment
from reporter.indexer import ClipMeta

POL = ActivityPolicy(version="activity-v0", gate_threshold=0.25)
PROD = ProducerInfo(host="host", run_id="run-1")


def _clip(cid):
    return ClipMeta(id=cid, camera_id="A", started_at="2026-07-14T01:00:00+00:00",
                    duration_sec=30.0, r2_key=f"k-{cid}", motion_score=0.1)


def _ga(clip_id, decision):
    return GateAssessment(
        result=PrelabelResult(True, 0.8, 12, "rf-detr-nano", "gecko_v2",
                              detected_objects=(), best_frame_ts=0.0, gecko_bbox=[1, 2, 3, 4]),
        motion=MotionMetrics(10, 0.9, 0.0, 0.0, 1.0, 0.0, 0.0, False),
        assessment=ActivityAssessment(decision, "r", {"policy_version": "pol-v0"}),
        provenance=GateProvenance("rf-detr-nano", "gecko_v2", "sha", 0.25, "s", "sv1", 12),
    )


def _dl_ok(key, dest):
    Path(dest).write_bytes(b"x")


def test_process_batch_all_ok_stores_each(tmp_path):
    sb = FakeSB({})
    clips = [_clip("c1"), _clip("c2")]
    stats = activity_worker.process_batch(
        sb, clips, detector=None, policy=POL, checkpoint_path="ck", producer=PROD,
        download_fn=_dl_ok,
        assess_fn=lambda path, det, pol, ck, clip_id: _ga(clip_id, "exclude_static"),
        store_fn=store_evidence_and_assessment,
    )
    assert stats["ok"] == 2 and stats["failed"] == 0
    assert stats["decisions"]["exclude_static"] == 2
    assert len(sb.store["clip_prelabels"]) == 2
    assert len(sb.store["clip_activity_assessments"]) == 2


def test_process_batch_isolates_one_failure():
    sb = FakeSB({})
    clips = [_clip("c1"), _clip("c2"), _clip("c3")]

    def dl(key, dest):
        if key == "k-c2":
            raise RuntimeError("download boom")
        Path(dest).write_bytes(b"x")

    stats = activity_worker.process_batch(
        sb, clips, None, POL, "ck", PROD,
        download_fn=dl,
        assess_fn=lambda path, det, pol, ck, clip_id: _ga(clip_id, "active"),
        store_fn=store_evidence_and_assessment,
    )
    assert stats["ok"] == 2 and stats["failed"] == 1  # c2 실패, c1/c3 진행
    assert stats["decisions"]["active"] == 2
    assert {r["clip_id"] for r in sb.store["clip_prelabels"]} == {"c1", "c3"}


def test_process_batch_counts_mixed_decisions():
    sb = FakeSB({})
    clips = [_clip("c1"), _clip("c2"), _clip("c3")]
    decisions = iter(["active", "exclude_absent", "unknown"])
    stats = activity_worker.process_batch(
        sb, clips, None, POL, "ck", PROD,
        download_fn=_dl_ok,
        assess_fn=lambda path, det, pol, ck, clip_id: _ga(clip_id, next(decisions)),
        store_fn=store_evidence_and_assessment,
    )
    assert stats["decisions"] == {"active": 1, "exclude_absent": 1, "exclude_static": 0, "unknown": 1}


def test_process_batch_reruns_idempotent():
    sb = FakeSB({})
    clips = [_clip("c1")]
    kw = dict(download_fn=_dl_ok,
              assess_fn=lambda path, det, pol, ck, clip_id: _ga(clip_id, "exclude_static"),
              store_fn=store_evidence_and_assessment)
    activity_worker.process_batch(sb, clips, None, POL, "ck", PROD, **kw)
    activity_worker.process_batch(sb, clips, None, POL, "ck", PROD, **kw)
    assert len(sb.store["clip_prelabels"]) == 1  # 멱등
    assert len(sb.store["clip_activity_assessments"]) == 1


def test_run_no_enabled_cameras_writes_nothing():
    sb = FakeSB({})  # 설정 없음 = allowlist 빈
    rc = activity_worker.run(sb=sb, now=datetime.fromisoformat("2026-07-14T05:00:00+00:00"))
    assert rc == 0
    assert "clip_prelabels" not in sb.store  # 아무것도 저장/제외 안 함


def _prelabel_row(threshold=0.25):
    return {
        "id": "pl-1", "clip_id": "c1", "model_version": "gecko_v2", "schema_version": SCHEMA_VERSION,
        "checkpoint_sha256": "sha", "threshold": threshold, "sampler_version": SAMPLER_VERSION,
        "gecko_visible": True, "visibility_confidence": 0.8, "frames_sampled": 12,
        "model_name": "rf-detr-nano", "best_frame_ts": 0.0, "gecko_bbox": [1, 2, 3, 4],
        "detected_objects": [{"type": "gecko", "confidence": 0.8, "bbox": [1, 2, 3, 4], "frame_ts": 0.0}],
        "motion_metrics": {"visible_frame_count": 10, "visible_frame_ratio": 0.9,
                           "max_bbox_center_disp": 0.0, "max_bbox_size_change": 0.0, "min_bbox_iou": 1.0,
                           "roi_flow_mag": 0.0, "global_bg_change": 0.0, "bbox_edge_clipped": False},
    }


def test_process_batch_reuses_prelabel_and_skips_inference():
    # 하드닝 3: identity(model/schema/checkpoint/threshold/sampler) 맞는 prelabel 이 이미 있으면
    # 다운로드/추론 없이 decide 만 재실행해 새 assessment 저장(policy reuse).
    sb = FakeSB({"clip_prelabels": [_prelabel_row(threshold=POL.gate_threshold)]})
    calls = {"dl": 0, "assess": 0}

    def dl(k, d):
        calls["dl"] += 1

    def assess(*a, **k):
        calls["assess"] += 1
        return _ga("c1", "active")

    stats = activity_worker.process_batch(
        sb, [_clip("c1")], detector=None, policy=POL, checkpoint_path="ck", producer=PROD,
        download_fn=dl, assess_fn=assess, store_fn=store_evidence_and_assessment,
        find_prelabel_fn=find_prelabel, reconstruct_fn=reconstruct_evidence, store_assessment_fn=store_assessment_only,
        model_version="gecko_v2", checkpoint_sha="sha",
    )
    assert calls["assess"] == 0 and calls["dl"] == 0  # 재추론/다운로드 스킵
    assert stats["reused"] == 1 and stats["ok"] == 0
    assert len(sb.store["clip_activity_assessments"]) == 1  # 재사용 evidence 로 새 assessment
    assert "clip_prelabels" in sb.store and len(sb.store["clip_prelabels"]) == 1  # evidence 는 그대로


def test_process_batch_infers_when_no_matching_prelabel():
    # identity 불일치(threshold 다름)면 재사용 안 하고 정상 추론 경로
    sb = FakeSB({"clip_prelabels": [_prelabel_row(threshold=0.10)]})  # POL.gate_threshold=0.25 와 불일치
    calls = {"assess": 0}

    def assess(*a, **k):
        calls["assess"] += 1
        return _ga("c1", "exclude_static")

    stats = activity_worker.process_batch(
        sb, [_clip("c1")], detector=None, policy=POL, checkpoint_path="ck", producer=PROD,
        download_fn=_dl_ok, assess_fn=assess, store_fn=store_evidence_and_assessment,
        find_prelabel_fn=find_prelabel, reconstruct_fn=reconstruct_evidence, store_assessment_fn=store_assessment_only,
        model_version="gecko_v2", checkpoint_sha="sha",
    )
    assert calls["assess"] == 1  # 매칭 없음 → 추론
    assert stats["ok"] == 1 and stats["reused"] == 0


def test_build_policy_versioned_presets(monkeypatch):
    # 모든 임계값이 policy version 에 묶인다(재현성). config 는 version 선택만.
    from reporter import config as cfg
    monkeypatch.setattr(cfg, "ACTIVITY_POLICY_VERSION", "activity-v1")
    p1 = activity_worker.build_activity_policy()
    assert p1.version == "activity-v1"
    assert p1.gate_threshold == 0.10          # audit: absent threshold 하향
    assert p1.roi_flow_active == 0.5          # audit: static 미세움직임 회수
    monkeypatch.setattr(cfg, "ACTIVITY_POLICY_VERSION", "activity-v0")
    p0 = activity_worker.build_activity_policy()
    assert p0.gate_threshold == 0.25 and p0.roi_flow_active == 2.0  # v0 원본(회귀 기준점)


# --- A단계: policy-version 정합성 guard ---
def _cs(cam, pv):
    return CameraFilterSetting(camera_id=cam, enabled=True, exclude_absent_enabled=False,
                               exclude_static_enabled=False, active_policy_version=pv)


def _srow(cam, pv):
    return {"camera_id": cam, "enabled": True, "exclude_absent_enabled": False,
            "exclude_static_enabled": False, "active_policy_version": pv}


_NOW = datetime.fromisoformat("2026-07-14T05:00:00+00:00")


def test_filter_by_policy_matches_and_logs_mismatch_null(capsys):
    settings = [_cs("cam-A", "activity-v1"), _cs("cam-B", "activity-v0"), _cs("cam-C", None)]
    matched = activity_worker._filter_by_policy(settings, "activity-v1")
    assert [s.camera_id for s in matched] == ["cam-A"]
    out = capsys.readouterr().out
    assert "camera cam-A po" not in out  # 일치는 skip 로그 없음
    assert "policy mismatch settings=activity-v0 worker=activity-v1 — skip" in out
    assert "policy mismatch settings=null worker=activity-v1 — skip" in out


def test_run_all_mismatch_skips_without_detector_load(monkeypatch):
    from reporter import config as cfg
    monkeypatch.setattr(cfg, "ACTIVITY_POLICY_VERSION", "activity-v1")
    sb = FakeSB({"camera_activity_filter_settings": [_srow("cam-A", "activity-v0"), _srow("cam-B", None)]})
    calls = {"det": 0, "dl": 0, "assess": 0}
    rc = activity_worker.run(
        sb=sb, now=_NOW,
        load_detector_fn=lambda *a, **k: calls.__setitem__("det", calls["det"] + 1),
        download_fn=lambda *a, **k: calls.__setitem__("dl", calls["dl"] + 1),
        assess_fn=lambda *a, **k: calls.__setitem__("assess", calls["assess"] + 1),
    )
    assert rc == 0
    assert calls == {"det": 0, "dl": 0, "assess": 0}  # detector 미로드·다운로드/추론 0회
    assert "clip_prelabels" not in sb.store and "clip_activity_assessments" not in sb.store


def test_run_null_policy_skips_without_store(monkeypatch):
    from reporter import config as cfg
    monkeypatch.setattr(cfg, "ACTIVITY_POLICY_VERSION", "activity-v1")
    sb = FakeSB({"camera_activity_filter_settings": [_srow("cam-A", None)]})
    calls = {"det": 0}
    rc = activity_worker.run(sb=sb, now=_NOW,
                             load_detector_fn=lambda *a, **k: calls.__setitem__("det", calls["det"] + 1))
    assert rc == 0 and calls["det"] == 0
    assert "clip_prelabels" not in sb.store


def test_run_processes_only_matching_policy(monkeypatch):
    from reporter import config as cfg
    monkeypatch.setattr(cfg, "ACTIVITY_POLICY_VERSION", "activity-v1")
    mc = [
        {"id": "a1", "camera_id": "cam-A", "started_at": "2026-07-14T01:00:00+00:00",
         "duration_sec": 10.0, "r2_key": "ka", "motion_score": 0.1},
        {"id": "b1", "camera_id": "cam-B", "started_at": "2026-07-14T01:00:00+00:00",
         "duration_sec": 10.0, "r2_key": "kb", "motion_score": 0.1},
    ]
    sb = FakeSB({"camera_activity_filter_settings": [_srow("cam-A", "activity-v1"), _srow("cam-B", "activity-v0")],
                 "motion_clips": mc})
    processed = []
    calls = {"det": 0}
    rc = activity_worker.run(
        sb=sb, now=_NOW,
        load_detector_fn=lambda *a, **k: (calls.__setitem__("det", 1), object())[1],
        download_fn=lambda key, dest: Path(dest).write_bytes(b"x"),
        assess_fn=lambda path, det, pol, ck, clip_id, **k: (processed.append(clip_id), _ga(clip_id, "exclude_static"))[1],
    )
    assert rc == 0
    assert calls["det"] == 1  # 일치 카메라 있어 detector 로드
    assert processed == ["a1"]  # cam-A(일치) clip 만 처리, cam-B(불일치) 제외
    assert {r["clip_id"] for r in sb.store["clip_prelabels"]} == {"a1"}
