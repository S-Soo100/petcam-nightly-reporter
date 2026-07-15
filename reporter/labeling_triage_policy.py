"""Gate 결과를 labeling triage 제안으로 바꾸는 순수 정책."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from gecko_vision_gate.provenance import GateProvenance

from reporter.gate_runner import GateAssessment
from reporter.labeling_triage_models import LabelingTriageClip, TriageSuggestion


def evidence_identity(clip_id: str, provenance: GateProvenance, policy_version: str) -> str:
    payload = {
        "clip_id": clip_id,
        "model_version": provenance.model_version,
        "checkpoint_sha256": provenance.checkpoint_sha256,
        "schema_version": provenance.schema_version,
        "threshold": provenance.threshold,
        "sampler_version": provenance.sampler_version,
        "frames_sampled": provenance.frames_sampled,
        "triage_policy_version": policy_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def suggest_from_gate(
    clip: LabelingTriageClip,
    gate: GateAssessment,
    policy_version: str,
) -> TriageSuggestion | None:
    mapping = {
        "active": ("label", "gate_active"),
        "exclude_absent": ("quarantine", "gate_absent"),
        "exclude_static": ("quarantine", "gate_static"),
    }
    mapped = mapping.get(gate.assessment.decision)
    if mapped is None:
        return None
    route, reason = mapped
    evidence = {
        "identity": evidence_identity(clip.id, gate.provenance, policy_version),
        "presence": {
            "gecko_visible": gate.result.gecko_visible,
            "visibility_confidence": gate.result.visibility_confidence,
        },
        "activity": {
            "decision": gate.assessment.decision,
            "reason_code": gate.assessment.reason_code,
            "measurements": gate.assessment.measurements,
        },
        "motion": asdict(gate.motion),
        "provenance": gate.provenance.to_dict(),
    }
    return TriageSuggestion(
        clip_id=clip.id,
        suggested_route=route,
        suggestion_reason=reason,
        suggestion_source="gate_activity_policy",
        policy_version=policy_version,
        evidence_snapshot=evidence,
    )
