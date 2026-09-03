"""KST 2026-07-15 이후 eligible/playable 영상의 GME historical job enqueuer.

기본은 dry-run이다. `--apply`일 때만 service-role RPC로 job을 만들며 원본/GT/R2는 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from supabase import create_client

from reporter import config, r2

BACKFILL_START = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)  # KST 07-15 00:00
MAX_BACKFILL_LIMIT = 50_000
ENGINE_SCHEMA_VERSION = "gme-shadow-v1"
ALGORITHM_VERSION = "gme-motion-v1"
EXCLUDED_SYSTEM_STATES = frozenset({"quarantined", "media_deleted"})
EXCLUDED_CLEANUP_STATES = frozenset({"quarantined", "media_deleted", "source_missing"})
INVENTORY_KEYS = (
    "scanned",
    "selected",
    "eligible",
    "excluded_test",
    "excluded_quarantined",
    "excluded_deleted",
    "excluded_source_missing",
    "excluded_existing",
    "excluded_invalid_metadata",
)


def _validate_identity(detector_identity: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", detector_identity) is None:
        raise ValueError("detector identity must be a lowercase SHA-256")


def _new_inventory() -> dict[str, int]:
    return {key: 0 for key in INVENTORY_KEYS}


def is_eligible_metadata(row: dict, *, exclusion_state: str | None, cleanup_state: str | None) -> bool:
    return bool(
        row.get("id") and row.get("camera_id") and row.get("started_at") and row.get("r2_key")
        and row.get("clip_purpose") == "production"
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


def _select_eligible_page(
    rows: list[dict], *, exclusions: dict[str, str], cleanup: dict[str, str],
    existing_clip_ids: set[str], r2_client, bucket: str, remaining: int,
    stats: dict[str, int], scan_all: bool = False,
) -> list[dict]:
    selected = []
    for row in rows:
        if len(selected) >= remaining and not scan_all:
            break
        stats["scanned"] += 1
        clip_id = row.get("id")
        exclusion_state = exclusions.get(clip_id)
        cleanup_state = cleanup.get(clip_id)
        if row.get("clip_purpose") != "production":
            stats["excluded_test"] += 1
        elif not all((clip_id, row.get("camera_id"), row.get("started_at"), row.get("r2_key"))):
            stats["excluded_invalid_metadata"] += 1
        elif exclusion_state == "quarantined" or cleanup_state == "quarantined":
            stats["excluded_quarantined"] += 1
        elif exclusion_state == "media_deleted" or cleanup_state == "media_deleted":
            stats["excluded_deleted"] += 1
        elif cleanup_state == "source_missing":
            stats["excluded_source_missing"] += 1
        elif clip_id in existing_clip_ids:
            stats["excluded_existing"] += 1
        elif not r2_object_exists(r2_client, bucket=bucket, key=row["r2_key"]):
            stats["excluded_source_missing"] += 1
        else:
            stats["eligible"] += 1
            if len(selected) < remaining:
                selected.append(row)
                stats["selected"] += 1
    return selected


def _existing_identity_clip_ids(sb, clip_ids: list[str], detector_identity: str) -> set[str]:
    if not clip_ids:
        return set()
    _validate_identity(detector_identity)
    existing = set()
    for table in ("gme_jobs", "gme_runs"):
        rows = (
            sb.table(table).select("clip_id").in_("clip_id", clip_ids)
            .eq("detector_identity", detector_identity)
            .eq("algorithm_version", ALGORITHM_VERSION).execute().data or []
        )
        existing.update(row["clip_id"] for row in rows if row.get("clip_id"))
    return existing


def _load_motion_page(sb, *, after_id: str | None, page_size: int) -> list[dict]:
    query = (
        sb.table("motion_clips").select("id,camera_id,started_at,r2_key,clip_purpose")
        .gte("started_at", BACKFILL_START.isoformat()).order("id").limit(page_size)
    )
    if after_id is not None:
        query = query.gt("id", after_id)
    return query.execute().data or []


def iter_eligible_batches(
    sb, *, limit: int, detector_identity: str, page_size: int = 500,
    r2_client=None, stats: dict[str, int] | None = None, scan_all: bool = False,
):
    if limit < 1 or limit > MAX_BACKFILL_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_BACKFILL_LIMIT}")
    _validate_identity(detector_identity)
    client = r2_client or r2.get_r2_client()
    inventory = stats if stats is not None else _new_inventory()
    selected_count = 0
    after_id = None
    while scan_all or selected_count < limit:
        rows = _load_motion_page(sb, after_id=after_id, page_size=page_size)
        if not rows:
            break
        ids = [row["id"] for row in rows]
        exclusions = _state_map(sb, "motion_clip_system_exclusions", ids)
        cleanup = _state_map(sb, "rba_owner_media_cleanup_items", ids)
        existing = _existing_identity_clip_ids(sb, ids, detector_identity)
        eligible_page = _select_eligible_page(
            rows,
            exclusions=exclusions,
            cleanup=cleanup,
            existing_clip_ids=existing,
            r2_client=client,
            bucket=config.R2_BUCKET,
            remaining=limit - selected_count,
            stats=inventory,
            scan_all=scan_all,
        )
        selected_count += len(eligible_page)
        if eligible_page:
            yield eligible_page
        after_id = rows[-1]["id"]
        if len(rows) < page_size:
            break


def load_eligible(
    sb, *, limit: int, detector_identity: str, page_size: int = 500,
    r2_client=None,
) -> list[dict]:
    selected = []
    for batch in iter_eligible_batches(
        sb, limit=limit, detector_identity=detector_identity,
        page_size=page_size, r2_client=r2_client,
    ):
        selected.extend(batch)
    return selected


def enqueue(
    sb, clip_ids: list[str], *, source: str, priority: int, apply: bool,
    detector_identity: str,
) -> int:
    _validate_identity(detector_identity)
    unique = list(dict.fromkeys(clip_ids))
    if not apply or not unique:
        return 0
    data = sb.rpc("fn_enqueue_gme_jobs", {
        "p_clip_ids": unique, "p_source": source, "p_priority": priority,
        "p_engine_schema_version": ENGINE_SCHEMA_VERSION, "p_algorithm_version": ALGORITHM_VERSION,
        "p_detector_identity": detector_identity,
    }).execute().data
    return int(data or 0)


def enqueue_batches(sb, batches, *, apply: bool, detector_identity: str) -> tuple[int, int]:
    selected = 0
    enqueued = 0
    for rows in batches:
        clip_ids = [row["id"] for row in rows]
        selected += len(clip_ids)
        enqueued += enqueue(
            sb, clip_ids, source="historical", priority=10, apply=apply,
            detector_identity=detector_identity,
        )
    return selected, enqueued


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GME eligible historical backfill enqueuer")
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    stats = _new_inventory()
    selected, count = enqueue_batches(
        sb,
        iter_eligible_batches(
            sb, limit=args.limit, detector_identity=config.GME_DETECTOR_IDENTITY,
            stats=stats, scan_all=not args.apply,
        ),
        apply=args.apply,
        detector_identity=config.GME_DETECTOR_IDENTITY,
    )
    print(json.dumps({
        "apply": bool(args.apply), "enqueued": count, "inventory": stats,
        "inventory_complete": not args.apply, "selected": selected,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
