"""activity_worker — batch 오케스트레이션(오류 격리·decision 집계·멱등 저장) + run 게이트.

process_batch 는 download/assess/store 를 주입받는 순수 오케스트레이션 → 실 R2/모델 없이 검증.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from _fakes import FakeSB

from gecko_vision_gate.activity_policy import ActivityAssessment, ActivityPolicy
from gecko_vision_gate.motion_evidence import MotionMetrics
from gecko_vision_gate.provenance import GateProvenance
from gecko_vision_gate.schema import PrelabelResult

from reporter import activity_worker
from reporter.activity_store import ProducerInfo, store_evidence_and_assessment
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
