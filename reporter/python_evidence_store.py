"""전 영상 Python evidence durable queue RPC client — claim/complete/fail/insert_run.

DB 계약(petcam-lab `migrations/2026-07-17_python_evidence_universal_worker.sql`)의 service_role RPC 를
얇게 감싼다. 원칙:
  - status/source/failure_code 는 **RPC 도달 전에 로컬 allowlist 검증**(잘못된 값이 DB 까지 안 감).
  - RPC 예외는 raw Supabase 에러(패스워드·URL 섞일 수 있음)를 숨기고 `EvidenceStoreError` 로 매핑
    (donts vlm §5.3 로그 위생). worker 는 타입만 보고 retry 여부를 판단한다.
  - row/dataclass 는 frozen(immutable) — evidence 는 사실이라 mutate 금지.

TS 로 치면 이 모듈은 stored-procedure 를 호출하는 typed repository 레이어다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from gecko_vision_gate.temporal_evidence import EVIDENCE_SCHEMA_VERSION, TemporalEvidence, TemporalPoint

# DB CHECK 와 1:1 동기화(migrations/2026-07-17_python_evidence_universal_worker.sql). drift 나면 회귀.
ALLOWED_STATUS = frozenset(
    {"queued", "processing", "succeeded", "failed_retryable", "failed_terminal"}
)
ALLOWED_SOURCE = frozenset({"live", "historical"})
ALLOWED_FAILURE_CODES = frozenset({
    "r2_download_failed", "decode_no_frames", "decode_insufficient_frames", "invalid_metadata",
    "detector_failed", "temporal_compute_failed", "db_transient", "db_error", "internal_error",
})
# Gate 7-column provenance identity (clip_prelabels provenance 컬럼과 동일 순서).
_IDENTITY_KEYS = (
    "model_name", "model_version", "checkpoint_sha256", "threshold",
    "sampler_version", "schema_version", "frames_sampled",
)


class EvidenceStoreError(RuntimeError):
    """DB/RPC 계층 오류의 위생 처리 래퍼 — raw 원문(secret/URL) 을 담지 않는다."""


class StaleJobError(EvidenceStoreError):
    """complete/fail 이 자기 lease 가 아니어서 거부됨(다른 worker 가 이미 회수). 재시도 대상 아님."""


@dataclass(frozen=True, slots=True)
class ProducerInfo:
    """이 evidence 를 만든 머신/실행/코드 참조 (관측·감사용, 비밀값 없음)."""

    host: str
    run_id: str
    code_ref: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceJob:
    """python_evidence_jobs 한 행 (claim 결과). frozen = 사실."""

    id: str
    clip_id: str
    source: str
    status: str
    priority: int
    evidence_schema_version: str
    algorithm_version: str
    attempt_count: int

    @classmethod
    def from_row(cls, row: dict) -> EvidenceJob:
        status = row.get("status")
        source = row.get("source")
        # 로컬 방어: DB 가 어쩌다 이상한 enum 을 줘도 worker 로직으로 흘리지 않는다.
        if status not in ALLOWED_STATUS:
            raise EvidenceStoreError(f"unknown job status: {status!r}")
        if source not in ALLOWED_SOURCE:
            raise EvidenceStoreError(f"unknown job source: {source!r}")
        return cls(
            id=row["id"],
            clip_id=row["clip_id"],
            source=source,
            status=status,
            priority=int(row.get("priority", 0)),
            evidence_schema_version=row.get("evidence_schema_version", EVIDENCE_SCHEMA_VERSION),
            algorithm_version=row.get("algorithm_version", ""),
            attempt_count=int(row.get("attempt_count", 0)),
        )


def source_prelabel_identity(prelabel: dict | None) -> str:
    """Gate 7-column identity 의 canonical JSON SHA-256. prelabel 없으면 literal 'none'.

    같은 prelabel(같은 모델/체크포인트/threshold/sampler) 재실행은 같은 hash → run 멱등 키의 일부.
    """
    if not prelabel:
        return "none"
    identity = {k: prelabel.get(k) for k in _IDENTITY_KEYS}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_rpc(sb, name: str, args: dict):
    """RPC 호출 + 예외 위생. raw 에러를 숨기고 RPC 이름만 노출."""
    try:
        return sb.rpc(name, args).execute().data
    except EvidenceStoreError:
        raise
    except Exception as e:  # noqa: BLE001 — 위생: 타입만 남기고 raw 원문(secret/URL) 폐기
        raise EvidenceStoreError(f"rpc failed: {name} ({type(e).__name__})") from None


def claim_jobs(sb, *, limit: int, worker_host: str, now: datetime) -> list[EvidenceJob]:
    """due 한 open job 을 live 우선·SKIP LOCKED 로 최대 limit 개 claim(→ processing). 없으면 []."""
    rows = _safe_rpc(sb, "fn_claim_python_evidence_jobs", {
        "p_limit": limit, "p_worker_host": worker_host, "p_now": now.isoformat(),
    })
    return [EvidenceJob.from_row(r) for r in (rows or [])]


def complete_job(sb, *, job_id: str, run_id: str, worker_host: str) -> None:
    """job 을 succeeded 로. 자기 lease 가 아니면(stale) StaleJobError."""
    ok = _safe_rpc(sb, "fn_complete_python_evidence_job", {
        "p_job_id": job_id, "p_run_id": run_id, "p_worker_host": worker_host,
    })
    if not ok:
        raise StaleJobError(f"complete rejected (stale lease): {job_id}")


def fail_job(sb, *, job_id: str, failure_code: str, retryable: bool,
             worker_host: str, now: datetime) -> None:
    """job 실패 기록. failure_code 는 allowlist 만(로컬 거부). retryable & attempt<max 면 backoff, 아니면 terminal."""
    if failure_code not in ALLOWED_FAILURE_CODES:
        raise ValueError(f"failure_code not in allowlist: {failure_code!r}")
    _safe_rpc(sb, "fn_fail_python_evidence_job", {
        "p_job_id": job_id, "p_failure_code": failure_code, "p_retryable": retryable,
        "p_worker_host": worker_host, "p_now": now.isoformat(),
    })


def _serialize_series(points: tuple[TemporalPoint, ...]) -> list[dict]:
    """TemporalPoint tuple → jsonb 배열(각 점 {t,value}). point cap 은 이미 코어에서 bound."""
    return [{"t": p.t, "value": p.value} for p in points]


def insert_run(sb, *, job: EvidenceJob, temporal: TemporalEvidence,
               prelabel: dict | None, producer: ProducerInfo) -> dict:
    """append-only 결과 원장에 1 run 삽입(멱등). 동일 identity 재실행은 기존 run 을 그대로 반환."""
    payload = {
        "clip_id": job.clip_id,
        "job_id": job.id,
        "prelabel_id": (prelabel.get("id") if prelabel else None),
        "evidence_schema_version": temporal.evidence_schema_version,
        "algorithm_version": temporal.algorithm_version,
        # Gate 7-column provenance (prelabel 있으면 그대로, 없으면 null)
        "model_name": (prelabel.get("model_name") if prelabel else None),
        "model_version": (prelabel.get("model_version") if prelabel else None),
        "checkpoint_sha256": (prelabel.get("checkpoint_sha256") if prelabel else None),
        "threshold": (prelabel.get("threshold") if prelabel else None),
        "sampler_version": (prelabel.get("sampler_version") if prelabel else None),
        "schema_version": (prelabel.get("schema_version") if prelabel else None),
        "frames_sampled": (prelabel.get("frames_sampled") if prelabel else None),
        "producer_host": producer.host,
        "producer_run_id": producer.run_id,
        "producer_code_ref": producer.code_ref,
        "level0_status": temporal.level0_status,
        "level1_status": temporal.level1_status,
        "decoded_frame_count": temporal.decoded_frame_count,
        "point_stride": temporal.point_stride,
        "metadata": temporal.motion_summary,  # metadata = 요약 수치(해상도/fps/duration 포함)
        "motion_summary": temporal.motion_summary,
        "global_motion_series": _serialize_series(temporal.global_motion_series),
        "roi_motion_series": _serialize_series(temporal.roi_motion_series),
        "spatial_dwell": temporal.spatial_dwell,
        "periodicity_summary": temporal.periodicity_summary,
        "motion_excursions": list(temporal.motion_excursions),
        "source_prelabel_identity": source_prelabel_identity(prelabel),
    }
    row = _safe_rpc(sb, "fn_insert_python_evidence_run", {"p_run": payload})
    return row
