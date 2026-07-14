"""activity_store — clip_prelabels(evidence) + clip_activity_assessments(decision) 멱등 저장.

멱등 키: prelabel=(clip_id, model_version, schema_version), assessment=(clip_id, policy_version).
같은 버전 재실행은 중복 0, 새 policy_version 은 새 assessment row(이력 보존, 지시문 §177).
"""

from _fakes import FakeSB

from gecko_vision_gate.activity_policy import ActivityAssessment
from gecko_vision_gate.motion_evidence import MotionMetrics
from gecko_vision_gate.provenance import GateProvenance
from gecko_vision_gate.schema import DetectedObject, PrelabelResult

from reporter.activity_store import ProducerInfo, store_evidence_and_assessment
from reporter.indexer import ClipMeta

PROD = ProducerInfo(host="mac-host", run_id="run-1")


def _clip(cid="c1"):
    return ClipMeta(id=cid, camera_id="A", started_at="2026-07-14T01:00:00+00:00",
                    duration_sec=30.0, r2_key="k1", motion_score=0.1)


def _result():
    return PrelabelResult(
        gecko_visible=True, visibility_confidence=0.85, frames_sampled=12,
        model_name="rf-detr-nano", model_version="gecko_v2",
        detected_objects=(DetectedObject("gecko", 0.85, [1, 2, 3, 4], 0.0),),
        best_frame_ts=0.0, gecko_bbox=[1, 2, 3, 4],
    )


def _motion():
    return MotionMetrics(10, 0.9, 0.0, 0.0, 1.0, 0.0, 0.0, False)


def _prov():
    # model_version=gecko_v2, schema_version=sv1 → prelabel 멱등 키
    return GateProvenance("rf-detr-nano", "gecko_v2", "sha256hex", 0.25, "samp-v1", "sv1", 12)


def _assess(dec="exclude_static", pv="pol-v0"):
    return ActivityAssessment(dec, "static_confirmed", {"policy_version": pv})


def test_stores_prelabel_and_assessment_shapes():
    sb = FakeSB({})
    out = store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(), _prov(), PROD)
    pl = sb.store["clip_prelabels"]
    aa = sb.store["clip_activity_assessments"]
    assert len(pl) == 1 and len(aa) == 1
    # evidence: provenance + gecko + per-frame detection + motion 손실없이
    assert pl[0]["clip_id"] == "c1"
    assert pl[0]["checkpoint_sha256"] == "sha256hex"
    assert pl[0]["threshold"] == 0.25
    assert pl[0]["gecko_visible"] is True
    assert pl[0]["detected_objects"] == [
        {"type": "gecko", "confidence": 0.85, "bbox": [1, 2, 3, 4], "frame_ts": 0.0}
    ]
    assert pl[0]["motion_metrics"]["visible_frame_count"] == 10
    assert pl[0]["producer_host"] == "mac-host"
    # decision: source prelabel 참조 + policy_version
    assert aa[0]["clip_id"] == "c1"
    assert aa[0]["decision"] == "exclude_static"
    assert aa[0]["policy_version"] == "pol-v0"
    assert aa[0]["prelabel_id"] == pl[0]["id"]
    assert out["decision"] == "exclude_static"


def test_idempotent_rerun_same_version_no_duplicates():
    sb = FakeSB({})
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(), _prov(), PROD)
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(), _prov(), PROD)
    assert len(sb.store["clip_prelabels"]) == 1
    assert len(sb.store["clip_activity_assessments"]) == 1


def test_new_policy_version_preserves_history():
    sb = FakeSB({})
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess("exclude_static", "pol-v0"), _prov(), PROD)
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess("active", "pol-v1"), _prov(), PROD)
    aa = sb.store["clip_activity_assessments"]
    assert len(aa) == 2  # 두 policy_version 이력 보존, 덮어쓰지 않음
    assert {r["policy_version"] for r in aa} == {"pol-v0", "pol-v1"}
    assert len(sb.store["clip_prelabels"]) == 1  # evidence 는 그대로
