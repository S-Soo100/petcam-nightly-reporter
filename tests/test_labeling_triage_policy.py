from gecko_vision_gate.activity_policy import ActivityAssessment
from gecko_vision_gate.motion_evidence import MotionMetrics
from gecko_vision_gate.provenance import GateProvenance
from gecko_vision_gate.schema import PrelabelResult

from reporter.gate_runner import GateAssessment
from reporter.labeling_triage_models import LabelingTriageClip
from reporter.labeling_triage_policy import suggest_from_gate


def _gate(decision: str) -> GateAssessment:
    return GateAssessment(
        result=PrelabelResult(
            gecko_visible=decision != "exclude_absent",
            visibility_confidence=0.81,
            frames_sampled=12,
            model_name="rfdetr",
            model_version="gecko-v2",
        ),
        motion=MotionMetrics(8, 0.667, 0.03, 0.1, 0.8, 0.7, 0.2, False),
        assessment=ActivityAssessment(
            decision=decision,
            reason_code={
                "active": "motion_observed",
                "exclude_absent": "no_gecko_detected",
                "exclude_static": "static_confirmed",
                "unknown": "sparse_detection",
            }[decision],
            measurements={"policy_version": "activity-v1"},
        ),
        provenance=GateProvenance(
            model_name="rfdetr",
            model_version="gecko-v2",
            checkpoint_sha256="a" * 64,
            threshold=0.1,
            sampler_version="even-uniform-v1",
            schema_version="gate-evidence-v1",
            frames_sampled=12,
        ),
    )


def _clip() -> LabelingTriageClip:
    return LabelingTriageClip(
        id="11111111-1111-1111-1111-111111111111",
        camera_id="22222222-2222-2222-2222-222222222222",
        started_at="2026-07-15T01:00:00+00:00",
        duration_sec=30.0,
        r2_key="secret/video.mp4",
    )


def test_maps_gate_decisions_to_safe_suggestions():
    expected = {
        "active": ("label", "gate_active"),
        "exclude_absent": ("quarantine", "gate_absent"),
        "exclude_static": ("quarantine", "gate_static"),
    }
    for decision, pair in expected.items():
        suggestion = suggest_from_gate(_clip(), _gate(decision), "labeling-triage-v1")
        assert suggestion is not None
        assert (suggestion.suggested_route, suggestion.suggestion_reason) == pair


def test_unknown_is_fail_open_without_a_suggestion():
    assert suggest_from_gate(_clip(), _gate("unknown"), "labeling-triage-v1") is None


def test_evidence_is_deterministic_and_excludes_sensitive_paths():
    first = suggest_from_gate(_clip(), _gate("active"), "labeling-triage-v1")
    second = suggest_from_gate(_clip(), _gate("active"), "labeling-triage-v1")
    assert first is not None and second is not None
    assert first.evidence_snapshot["identity"] == second.evidence_snapshot["identity"]
    assert set(first.evidence_snapshot) == {"identity", "presence", "activity", "motion", "provenance"}
    serialized = str(first.evidence_snapshot)
    for forbidden in ("secret/video.mp4", "/Users/", "producer_host", "checkpoint_path"):
        assert forbidden not in serialized
