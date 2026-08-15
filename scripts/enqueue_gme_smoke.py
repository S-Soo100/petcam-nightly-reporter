"""GME operational smoke용 실제 eligible 영상 정확히 10건 selector/enqueuer."""

from __future__ import annotations

import argparse

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GME 10-real-clip smoke enqueuer")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    selected = select_smoke(load_eligible(sb, limit=200), limit=10)
    if len(selected) != 10:
        print(f"[gme-smoke] preflight failed eligible={len(selected)}/10")
        return 2
    count = enqueue(
        sb, [row["id"] for row in selected], source="smoke", priority=90, apply=args.apply,
        detector_identity=config.GME_DETECTOR_IDENTITY,
    )
    print(f"[gme-smoke] selected=10 enqueued={count} apply={int(args.apply)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
