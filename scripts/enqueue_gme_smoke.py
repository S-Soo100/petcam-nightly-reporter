"""GME operational smoke용 실제 eligible 영상 정확히 10건 selector/enqueuer."""

from __future__ import annotations

import argparse
import re

from supabase import create_client

from reporter import config
from scripts.enqueue_gme_backfill import enqueue, load_eligible


def select_smoke(rows: list[dict], *, limit: int = 10) -> list[dict]:
    unique = {row["id"]: row for row in rows}
    ordered = sorted(unique.values(), key=lambda row: (row["started_at"], row["camera_id"], row["id"]))
    selected: list[dict] = []
    seen_strata = set()
    for row in ordered:
        stratum = (row["camera_id"], row["started_at"][:10])
        if stratum not in seen_strata:
            selected.append(row)
            seen_strata.add(stratum)
            if len(selected) == limit:
                return selected
    for row in ordered:
        if row not in selected:
            selected.append(row)
            if len(selected) == limit:
                break
    return selected


def select_recovery_smoke(
    rows: list[dict], *, existing_clip_ids: set[str], limit: int = 10,
) -> list[dict]:
    return select_smoke(
        [row for row in rows if row.get("id") not in existing_clip_ids],
        limit=limit,
    )


def validate_recovery_incident(
    jobs: list[dict], *, detector_identity: str,
) -> set[str]:
    if re.fullmatch(r"[0-9a-f]{64}", detector_identity) is None:
        raise ValueError("detector identity must be a lowercase SHA-256")
    clip_ids = {job.get("clip_id") for job in jobs}
    valid = len(jobs) == 10 and len(clip_ids) == 10 and None not in clip_ids
    for job in jobs:
        valid = valid and all((
            job.get("source") == "smoke",
            job.get("status") == "failed_terminal",
            job.get("failure_code") == "invalid_metadata",
            job.get("result_run_id") is None,
            job.get("detector_identity") == detector_identity,
        ))
    if not valid:
        raise ValueError("recovery incident contract mismatch")
    return clip_ids


def load_existing_identity_jobs(sb, *, detector_identity: str) -> list[dict]:
    if re.fullmatch(r"[0-9a-f]{64}", detector_identity) is None:
        raise ValueError("detector identity must be a lowercase SHA-256")
    return (
        sb.table("gme_jobs")
        .select("id,clip_id,source,status,failure_code,result_run_id,detector_identity")
        .eq("detector_identity", detector_identity)
        .execute().data or []
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GME 10-real-clip smoke enqueuer")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recovery-after-claim-incident", action="store_true")
    args = parser.parse_args(argv)
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    identity = config.GME_DETECTOR_IDENTITY
    existing = load_existing_identity_jobs(sb, detector_identity=identity)
    if args.recovery_after_claim_incident:
        try:
            existing_clip_ids = validate_recovery_incident(existing, detector_identity=identity)
        except ValueError as exc:
            print(f"[gme-smoke] preflight failed: {exc}")
            return 2
        selected = select_recovery_smoke(
            load_eligible(sb, limit=500), existing_clip_ids=existing_clip_ids, limit=10,
        )
    else:
        if existing:
            print(f"[gme-smoke] preflight failed existing_identity_jobs={len(existing)}")
            return 2
        selected = select_smoke(load_eligible(sb, limit=200), limit=10)
    if len(selected) != 10:
        print(f"[gme-smoke] preflight failed eligible={len(selected)}/10")
        return 2
    count = enqueue(
        sb, [row["id"] for row in selected], source="smoke", priority=90, apply=args.apply,
        detector_identity=identity,
    )
    if args.apply and count != 10:
        print(f"[gme-smoke] apply failed inserted={count}/10")
        return 2
    print(f"[gme-smoke] selected=10 enqueued={count} apply={int(args.apply)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
