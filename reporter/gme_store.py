"""GME durable queue와 append-only run RPC의 typed repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

ALLOWED_STATUS = frozenset({"queued", "processing", "succeeded", "failed_retryable", "failed_terminal"})
ALLOWED_SOURCE = frozenset({"smoke", "live", "historical"})
ALLOWED_FAILURE_CODES = frozenset({
    "r2_download_failed", "source_media_missing", "r2_access_denied", "decode_no_frames",
    "invalid_metadata", "detector_failed", "gme_compute_failed", "artifact_upload_failed",
    "db_transient", "internal_error",
})


class GMEStoreError(RuntimeError):
    pass


class StaleGMEJobError(GMEStoreError):
    pass


@dataclass(frozen=True, slots=True)
class GMEJob:
    id: str
    clip_id: str
    source: str
    priority: int
    engine_schema_version: str
    algorithm_version: str
    detector_identity: str
    status: str
    attempt_count: int

    @classmethod
    def from_row(cls, row: dict) -> "GMEJob":
        if row.get("source") not in ALLOWED_SOURCE or row.get("status") not in ALLOWED_STATUS:
            raise GMEStoreError("invalid job enum")
        identity = row.get("detector_identity", "")
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise GMEStoreError("invalid detector identity")
        return cls(
            row["id"], row["clip_id"], row["source"], int(row["priority"]),
            row["engine_schema_version"], row["algorithm_version"], identity,
            row["status"], int(row.get("attempt_count", 0)),
        )


def _rpc(sb, name: str, args: dict):
    try:
        return sb.rpc(name, args).execute().data
    except GMEStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - raw URL/key/DB message는 로그로 전파하지 않는다.
        raise GMEStoreError(f"rpc failed: {name} ({type(exc).__name__})") from None


def claim_jobs(
    sb,
    *,
    limit: int,
    worker_host: str,
    now: datetime,
    include_historical: bool,
    detector_identity: str,
) -> list[GMEJob]:
    if re.fullmatch(r"[0-9a-f]{64}", detector_identity) is None:
        raise ValueError("detector identity must be a lowercase SHA-256")
    rows = _rpc(sb, "fn_claim_gme_jobs_for_detector", {
        "p_limit": limit, "p_worker_host": worker_host, "p_now": now.isoformat(),
        "p_include_historical": include_historical,
        "p_detector_identity": detector_identity,
    })
    return [GMEJob.from_row(row) for row in (rows or [])]


def complete_job(sb, *, job_id: str, run_id: str, worker_host: str) -> None:
    ok = _rpc(sb, "fn_complete_gme_job", {
        "p_job_id": job_id, "p_run_id": run_id, "p_worker_host": worker_host,
    })
    if not ok:
        raise StaleGMEJobError("complete rejected by stale lease")


def fail_job(sb, *, job_id: str, failure_code: str, retryable: bool, worker_host: str, now: datetime) -> None:
    if failure_code not in ALLOWED_FAILURE_CODES:
        raise ValueError("failure code not allowlisted")
    ok = _rpc(sb, "fn_fail_gme_job", {
        "p_job_id": job_id, "p_failure_code": failure_code, "p_retryable": retryable,
        "p_worker_host": worker_host, "p_now": now.isoformat(),
    })
    if ok is False:
        raise StaleGMEJobError("failure rejected by stale lease")


def insert_run(sb, payload: dict) -> dict:
    intervals = payload.get("state_intervals")
    quality = payload.get("tracking_quality")
    if not isinstance(intervals, list) or len(intervals) > 10000 or not isinstance(quality, dict):
        raise GMEStoreError("malformed bounded run payload")
    for interval in intervals:
        if not isinstance(interval, dict) or interval.get("state") not in {
            "moving", "static", "not_visible", "unknown", "camera_motion",
        }:
            raise GMEStoreError("malformed state interval")
    row = _rpc(sb, "fn_insert_gme_run", {"p_run": payload})
    if not isinstance(row, dict) or not row.get("id"):
        raise GMEStoreError("run insert returned no row")
    return row


def operational_stats(sb, *, now: datetime) -> dict:
    value = _rpc(sb, "fn_gme_operational_stats", {"p_now": now.isoformat()})
    if not isinstance(value, dict):
        raise GMEStoreError("operational stats malformed")
    return value


def load_clip_r2_keys(sb, clip_ids) -> dict[str, str]:
    ids = list(clip_ids)
    if not ids:
        return {}
    try:
        rows = sb.table("motion_clips").select("id,r2_key").in_("id", ids).execute().data
    except Exception as exc:  # noqa: BLE001
        raise GMEStoreError(f"clip lookup failed ({type(exc).__name__})") from None
    return {row["id"]: row["r2_key"] for row in (rows or []) if row.get("r2_key")}
