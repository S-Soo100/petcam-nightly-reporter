"""activity_store — clip_prelabels(evidence) + clip_activity_assessments(decision) 멱등 저장.

멱등 키: prelabel=(clip_id, model_version, schema_version), assessment=(clip_id, policy_version).
같은 버전 재실행은 중복 0, 새 policy_version 은 새 assessment row(이력 보존, 지시문 §177).
"""

from tests._fakes import FakeSB

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


def test_different_threshold_makes_separate_evidence():
    # 같은 clip 을 threshold 0.25 / 0.10 로 뽑은 evidence 는 identity 가 달라 별도 row (덮어쓰지 않음).
    # audit 이 증명: 0.25=no_gecko, 0.10=gecko conf 0.14~0.21 → 둘 다 보존해야 비교 가능.
    sb = FakeSB({})
    prov_025 = GateProvenance("rf-detr-nano", "gecko_v2", "sha", 0.25, "samp-v1", "sv1", 12)
    prov_010 = GateProvenance("rf-detr-nano", "gecko_v2", "sha", 0.10, "samp-v1", "sv1", 12)
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(pv="p025"), prov_025, PROD)
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(pv="p010"), prov_010, PROD)
    assert len(sb.store["clip_prelabels"]) == 2
    assert {r["threshold"] for r in sb.store["clip_prelabels"]} == {0.25, 0.10}


def test_different_checkpoint_makes_separate_evidence():
    # checkpoint sha256 이 다르면(재학습/다른 아티팩트) 별도 evidence.
    sb = FakeSB({})
    prov_a = GateProvenance("rf-detr-nano", "gecko_v2", "sha-A", 0.25, "samp-v1", "sv1", 12)
    prov_b = GateProvenance("rf-detr-nano", "gecko_v2", "sha-B", 0.25, "samp-v1", "sv1", 12)
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(pv="pa"), prov_a, PROD)
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(pv="pb"), prov_b, PROD)
    assert len(sb.store["clip_prelabels"]) == 2


def test_different_frames_sampled_makes_separate_evidence():
    # sampler config = sampler_version + frames_sampled. 샘플 수가 다르면 별도 evidence(다른 입력).
    sb = FakeSB({})
    prov_12 = GateProvenance("rf-detr-nano", "gecko_v2", "sha", 0.25, "samp-v1", "sv1", 12)
    prov_20 = GateProvenance("rf-detr-nano", "gecko_v2", "sha", 0.25, "samp-v1", "sv1", 20)
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(pv="p12"), prov_12, PROD)
    store_evidence_and_assessment(sb, _clip(), _result(), _motion(), _assess(pv="p20"), prov_20, PROD)
    assert len(sb.store["clip_prelabels"]) == 2
    assert {r["frames_sampled"] for r in sb.store["clip_prelabels"]} == {12, 20}
