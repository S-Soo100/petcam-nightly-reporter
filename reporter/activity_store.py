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
from gecko_vision_gate.schema import DetectedObject, PrelabelResult

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
    # evidence identity = clip + 아티팩트(checkpoint sha256/model_version) + threshold + sampler + schema.
    # threshold 를 identity 에 넣어야 0.25/0.10 evidence 가 덮이지 않고 둘 다 보존된다(audit 교훈).
    prow = (
        sb.table("clip_prelabels")
        .upsert(prelabel_row,
                on_conflict="clip_id,model_version,schema_version,checkpoint_sha256,threshold,sampler_version")
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


def find_prelabel(
    sb,
    clip_id: str,
    model_version: str,
    schema_version: str,
    checkpoint_sha256: str,
    threshold: float,
    sampler_version: str,
) -> dict | None:
    """evidence identity 로 기존 prelabel 조회 (policy reuse: 재추론 스킵 판단용). 없으면 None."""
    rows = (
        sb.table("clip_prelabels")
        .select("*")
        .eq("clip_id", clip_id)
        .eq("model_version", model_version)
        .eq("schema_version", schema_version)
        .eq("checkpoint_sha256", checkpoint_sha256)
        .eq("threshold", threshold)
        .eq("sampler_version", sampler_version)
        .execute()
        .data
    )
    return rows[0] if rows else None


def reconstruct_evidence(row: dict) -> tuple[PrelabelResult, MotionMetrics]:
    """clip_prelabels row → (PrelabelResult, MotionMetrics). policy reuse 시 decide 재실행용."""
    objs = tuple(
        DetectedObject(o["type"], o["confidence"], o["bbox"], o["frame_ts"])
        for o in (row.get("detected_objects") or [])
    )
    result = PrelabelResult(
        gecko_visible=row["gecko_visible"],
        visibility_confidence=row["visibility_confidence"],
        frames_sampled=row["frames_sampled"],
        model_name=row["model_name"],
        model_version=row["model_version"],
        detected_objects=objs,
        best_frame_ts=row.get("best_frame_ts"),
        gecko_bbox=row.get("gecko_bbox"),
    )
    motion = MotionMetrics(**row["motion_metrics"])
    return result, motion


def store_assessment_only(sb, clip_id: str, prelabel_id, assessment: ActivityAssessment, producer: ProducerInfo) -> dict:
    """기존 prelabel 을 재사용해 새 assessment 만 저장(policy_version 만 바뀐 재평가)."""
    row = {
        "clip_id": clip_id,
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
        .upsert(row, on_conflict="clip_id,policy_version")
        .execute()
    )
    return {"prelabel_id": prelabel_id, "decision": assessment.decision, "reused": True}
