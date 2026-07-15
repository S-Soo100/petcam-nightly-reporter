#!/usr/bin/env python3
"""과거 VLM 백필 한 source night의 30개 후보를 DB write·Claude 호출 없이 미리 본다."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supabase import create_client  # noqa: E402

from reporter import config  # noqa: E402
from reporter.activity_worker import acquire_activity_lock, release_activity_lock  # noqa: E402
from reporter.vlm_backfill_selector import source_nights  # noqa: E402
from reporter.vlm_backfill_worker import choose_target_camera, prepare_wave  # noqa: E402


def _source_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("source-date는 YYYY-MM-DD 형식이어야 함") from exc
    if parsed not in source_nights():
        allowed = f"{source_nights()[0]}..{source_nights()[-1]}"
        raise argparse.ArgumentTypeError(f"source-date 허용 범위는 {allowed}")
    return parsed


def main(
    argv=None, *, sb=None, prepare_fn=prepare_wave,
    acquire_lock_fn=acquire_activity_lock, release_lock_fn=release_activity_lock,
) -> int:
    parser = argparse.ArgumentParser(description="VLM 240개 백필 read-only 후보 preview")
    parser.add_argument("--source-date", required=True, type=_source_date)
    args = parser.parse_args(argv)
    sb = sb or create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    camera_id = choose_target_camera(sb, source_nights())
    lock = acquire_lock_fn()
    if lock is None:
        raise RuntimeError("activity worker busy; preview deferred")
    try:
        wave = prepare_fn(sb, args.source_date, camera_id, persist=False)
    finally:
        release_lock_fn(lock)
    print(json.dumps(wave.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
