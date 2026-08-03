"""KST 2026-07-15 이후 eligible/playable 영상의 GME historical job enqueuer.

기본은 dry-run이다. `--apply`일 때만 service-role RPC로 job을 만들며 원본/GT/R2는 쓰지 않는다.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from supabase import create_client

from reporter import config, r2

BACKFILL_START = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)  # KST 07-15 00:00
MAX_BACKFILL_LIMIT = 50_000
ENGINE_SCHEMA_VERSION = "gme-shadow-v1"
ALGORITHM_VERSION = "gme-motion-v0"
DETECTOR_IDENTITY = "7997e853e851ac6592e03d13e7d5098ebfcbcb49b408077d83d7d6359df60a2a"
EXCLUDED_SYSTEM_STATES = frozenset({"quarantined", "media_deleted"})
EXCLUDED_CLEANUP_STATES = frozenset({"quarantined", "media_deleted", "source_missing"})


def is_eligible_metadata(row: dict, *, exclusion_state: str | None, cleanup_state: str | None) -> bool:
    return bool(
        row.get("id") and row.get("camera_id") and row.get("started_at") and row.get("r2_key")
        and exclusion_state not in EXCLUDED_SYSTEM_STATES
        and cleanup_state not in EXCLUDED_CLEANUP_STATES
    )


def _state_map(sb, table: str, clip_ids: list[str]) -> dict[str, str]:
    if not clip_ids:
        return {}
    rows = sb.table(table).select("clip_id,state").in_("clip_id", clip_ids).execute().data or []
    return {row["clip_id"]: row["state"] for row in rows}


def r2_object_exists(client, *, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 - unavailable/denied 모두 enqueue 제외, 원문 로그 금지
        return False


def _load_motion_page(sb, *, after_id: str | None, page_size: int) -> list[dict]:
    query = (
        sb.table("motion_clips").select("id,camera_id,started_at,r2_key")
        .gte("started_at", BACKFILL_START.isoformat()).order("id").limit(page_size)
    )
    if after_id is not None:
        query = query.gt("id", after_id)
    return query.execute().data or []


def iter_eligible_batches(sb, *, limit: int, page_size: int = 500, r2_client=None):
    if limit < 1 or limit > MAX_BACKFILL_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_BACKFILL_LIMIT}")
    client = r2_client or r2.get_r2_client()
    selected_count = 0
    after_id = None
    while selected_count < limit:
        rows = _load_motion_page(sb, after_id=after_id, page_size=page_size)
        if not rows:
            break
        ids = [row["id"] for row in rows]
        exclusions = _state_map(sb, "motion_clip_system_exclusions", ids)
        cleanup = _state_map(sb, "rba_owner_media_cleanup_items", ids)
        eligible_page = []
        for row in rows:
            if is_eligible_metadata(row, exclusion_state=exclusions.get(row["id"]), cleanup_state=cleanup.get(row["id"])):
                if r2_object_exists(client, bucket=config.R2_BUCKET, key=row["r2_key"]):
                    eligible_page.append(row)
                    selected_count += 1
                    if selected_count >= limit:
                        break
        if eligible_page:
            yield eligible_page
        after_id = rows[-1]["id"]
        if len(rows) < page_size:
            break


def load_eligible(sb, *, limit: int, page_size: int = 500, r2_client=None) -> list[dict]:
    selected = []
    for batch in iter_eligible_batches(sb, limit=limit, page_size=page_size, r2_client=r2_client):
        selected.extend(batch)
    return selected


def enqueue(sb, clip_ids: list[str], *, source: str, priority: int, apply: bool) -> int:
    unique = list(dict.fromkeys(clip_ids))
    if not apply or not unique:
        return 0
    data = sb.rpc("fn_enqueue_gme_jobs", {
        "p_clip_ids": unique, "p_source": source, "p_priority": priority,
        "p_engine_schema_version": ENGINE_SCHEMA_VERSION, "p_algorithm_version": ALGORITHM_VERSION,
        "p_detector_identity": DETECTOR_IDENTITY,
    }).execute().data
    return int(data or 0)


def enqueue_batches(sb, batches, *, apply: bool) -> tuple[int, int]:
    selected = 0
    enqueued = 0
    for rows in batches:
        clip_ids = [row["id"] for row in rows]
        selected += len(clip_ids)
        enqueued += enqueue(sb, clip_ids, source="historical", priority=10, apply=apply)
    return selected, enqueued


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GME eligible historical backfill enqueuer")
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    selected, count = enqueue_batches(
        sb,
        iter_eligible_batches(sb, limit=args.limit),
        apply=args.apply,
    )
    print(f"[gme-backfill] eligible={selected} enqueued={count} apply={int(args.apply)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
