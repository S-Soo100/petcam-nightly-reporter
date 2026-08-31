"""GME 10-real-clip smoke의 read-only operational audit."""

from __future__ import annotations

import re

from supabase import create_client

from reporter import config, r2
from reporter.gme_artifacts import DEBUG_PREFIX, PERMANENT_PREFIX


def evaluate_smoke(jobs: list[dict], runs: list[dict], *, detector_identity: str, head_fn) -> dict:
    if re.fullmatch(r"[0-9a-f]{64}", detector_identity) is None:
        raise ValueError("detector identity must be a lowercase SHA-256")
    run_by_id = {row["id"]: row for row in runs}
    complete = 0
    artifact_count = 0
    clip_ids = set()
    valid = len(jobs) == 10
    for job in jobs:
        if job.get("detector_identity") != detector_identity:
            valid = False
        clip_ids.add(job.get("clip_id"))
        run = run_by_id.get(job.get("result_run_id"))
        if job.get("status") != "succeeded" or run is None or run.get("job_id") != job.get("id"):
            valid = False
            continue
        complete += 1
        for key_field, sha_field, prefix in (
            ("permanent_artifact_key", "permanent_artifact_sha256", PERMANENT_PREFIX),
            ("debug_artifact_key", "debug_artifact_sha256", DEBUG_PREFIX),
        ):
            key, digest = run.get(key_field), run.get(sha_field)
            if not isinstance(key, str) or not key.startswith(prefix) or not head_fn(key, digest):
                valid = False
            else:
                artifact_count += 1
    valid = valid and complete == 10 and len(clip_ids) == 10 and artifact_count == 20
    return {"complete": complete, "unique_clips": len(clip_ids), "artifacts": artifact_count, "ok": valid}


def evaluate_recovery_smoke(
    jobs: list[dict], runs: list[dict], *, detector_identity: str, head_fn,
) -> dict:
    if re.fullmatch(r"[0-9a-f]{64}", detector_identity) is None:
        raise ValueError("detector identity must be a lowercase SHA-256")
    run_by_id = {row.get("id"): row for row in runs}
    clip_ids = {job.get("clip_id") for job in jobs}
    incident = [job for job in jobs if job.get("status") == "failed_terminal"]
    complete_jobs = [job for job in jobs if job.get("status") == "succeeded"]
    valid = all((
        len(jobs) == 20,
        len(clip_ids) == 20,
        None not in clip_ids,
        len(incident) == 10,
        len(complete_jobs) == 10,
        len(run_by_id) == 10,
    ))

    for job in incident:
        valid = valid and all((
            job.get("source") == "smoke",
            job.get("failure_code") == "invalid_metadata",
            job.get("result_run_id") is None,
            job.get("detector_identity") == detector_identity,
        ))

    artifact_count = 0
    complete = 0
    for job in complete_jobs:
        valid = valid and all((
            job.get("source") == "smoke",
            job.get("failure_code") is None,
            job.get("detector_identity") == detector_identity,
        ))
        run = run_by_id.get(job.get("result_run_id"))
        if run is None or any((
            run.get("job_id") != job.get("id"),
            run.get("status") != "ok",
            run.get("detector_identity") != detector_identity,
        )):
            valid = False
            continue
        complete += 1
        for key_field, sha_field, bytes_field, prefix in (
            ("permanent_artifact_key", "permanent_artifact_sha256", "permanent_artifact_bytes", PERMANENT_PREFIX),
            ("debug_artifact_key", "debug_artifact_sha256", "debug_artifact_bytes", DEBUG_PREFIX),
        ):
            key = run.get(key_field)
            digest = run.get(sha_field)
            byte_count = run.get(bytes_field)
            metadata_valid = all((
                isinstance(key, str),
                isinstance(key, str) and key.startswith(prefix),
                isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
                isinstance(byte_count, int) and byte_count > 0,
            ))
            if not metadata_valid or not head_fn(key, digest):
                valid = False
            else:
                artifact_count += 1

    valid = valid and complete == 10 and artifact_count == 20
    return {
        "incident": len(incident),
        "complete": complete,
        "unique_clips": len(clip_ids),
        "artifacts": artifact_count,
        "ok": valid,
    }


def main() -> int:
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    identity = config.GME_DETECTOR_IDENTITY
    if re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        raise ValueError("detector identity must be configured as a lowercase SHA-256")
    jobs = (
        sb.table("gme_jobs")
        .select("id,clip_id,source,status,failure_code,result_run_id,detector_identity")
        .eq("source", "smoke")
        .eq("detector_identity", identity)
        .execute().data or []
    )
    run_ids = [row["result_run_id"] for row in jobs if row.get("result_run_id")]
    runs = [] if not run_ids else (
        sb.table("gme_runs")
        .select(
            "id,job_id,status,detector_identity,"
            "permanent_artifact_key,permanent_artifact_sha256,permanent_artifact_bytes,"
            "debug_artifact_key,debug_artifact_sha256,debug_artifact_bytes"
        )
        .in_("id", run_ids).execute().data or []
    )
    client = r2.get_r2_client()

    def head(key, digest):
        try:
            response = client.head_object(Bucket=config.R2_BUCKET, Key=key)
            return response.get("Metadata", {}).get("sha256") == digest
        except Exception:  # noqa: BLE001 - 원문 비노출
            return False

    report = evaluate_recovery_smoke(jobs, runs, detector_identity=identity, head_fn=head)
    print(
        f"[gme-audit] incident={report['incident']}/10 complete={report['complete']}/10 "
        f"unique={report['unique_clips']}/20 "
        f"artifacts={report['artifacts']}/20 ok={int(report['ok'])}"
    )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
