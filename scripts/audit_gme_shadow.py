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


def main() -> int:
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    identity = config.GME_CHECKPOINT_SHA256
    if re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        raise ValueError("detector identity must be configured as a lowercase SHA-256")
    jobs = (
        sb.table("gme_jobs")
        .select("id,clip_id,status,result_run_id,detector_identity")
        .eq("source", "smoke")
        .eq("detector_identity", identity)
        .execute().data or []
    )
    run_ids = [row["result_run_id"] for row in jobs if row.get("result_run_id")]
    runs = [] if not run_ids else (
        sb.table("gme_runs")
        .select("id,job_id,permanent_artifact_key,permanent_artifact_sha256,debug_artifact_key,debug_artifact_sha256")
        .in_("id", run_ids).execute().data or []
    )
    client = r2.get_r2_client()

    def head(key, digest):
        try:
            response = client.head_object(Bucket=config.R2_BUCKET, Key=key)
            return response.get("Metadata", {}).get("sha256") == digest
        except Exception:  # noqa: BLE001 - 원문 비노출
            return False

    report = evaluate_smoke(jobs, runs, detector_identity=identity, head_fn=head)
    print(
        f"[gme-audit] complete={report['complete']}/10 unique={report['unique_clips']} "
        f"artifacts={report['artifacts']}/20 ok={int(report['ok'])}"
    )
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
