"""clip_prelabels(evidence) + clip_activity_assessments(decision) service_role 저장.

evidence 와 decision 을 DB 로 분리(지시문 §97). 둘 다 멱등 upsert:
  - prelabel: on_conflict (clip_id, model_version, schema_version) → 같은 Gate 버전 재실행 무해
  - assessment: on_conflict (clip_id, policy_version) → 같은 정책 재평가 무해, 새 정책은 새 row
원본 motion_clips/R2 는 절대 건드리지 않는다(지시문 §90).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from gecko_vision_gate.activity_policy import ActivityAssessment
from gecko_vision_gate.motion_evidence import MotionMetrics
from gecko_vision_gate.provenance import GateProvenance
from gecko_vision_gate.schema import PrelabelResult

from reporter.indexer import ClipMeta


@dataclass(frozen=True, slots=True)
class ProducerInfo:
    """어느 머신/실행이 이 evidence 를 만들었나 (관측/감사용, 비밀값 없음)."""

    host: str
    run_id: str


def store_evidence_and_assessment(
    sb,
    clip: ClipMeta,
    result: PrelabelResult,
    motion: MotionMetrics,
    assessment: ActivityAssessment,
    provenance: GateProvenance,
    producer: ProducerInfo,
) -> dict:
    """clip 1건의 evidence + decision 저장. 반환 {prelabel_id, decision}."""
    prelabel_row = {
        "clip_id": clip.id,
        **provenance.to_dict(),
        "gecko_visible": result.gecko_visible,
        "visibility_confidence": result.visibility_confidence,
        "best_frame_ts": result.best_frame_ts,
        "gecko_bbox": result.gecko_bbox,
        "detected_objects": [asdict(o) for o in result.detected_objects],
        "motion_metrics": asdict(motion),
        "producer_host": producer.host,
        "producer_run_id": producer.run_id,
    }
    prow = (
        sb.table("clip_prelabels")
        .upsert(prelabel_row, on_conflict="clip_id,model_version,schema_version")
        .execute()
        .data
    )
    prelabel_id = prow[0]["id"]

    assessment_row = {
        "clip_id": clip.id,
        "prelabel_id": prelabel_id,
        "decision": assessment.decision,
        "reason_code": assessment.reason_code,
        "measurements": assessment.measurements,
        "policy_version": assessment.measurements["policy_version"],
        "producer_host": producer.host,
        "producer_run_id": producer.run_id,
    }
    (
        sb.table("clip_activity_assessments")
        .upsert(assessment_row, on_conflict="clip_id,policy_version")
        .execute()
    )
    return {"prelabel_id": prelabel_id, "decision": assessment.decision}
