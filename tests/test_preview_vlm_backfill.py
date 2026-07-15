import json
from types import SimpleNamespace

import pytest

from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION, bucket_plans, source_nights
from scripts.preview_vlm_backfill import main
from tests._fakes import FakeSB


def _preview_wave(source_date, camera_id):
    selected = [
        {"clip": f"clip-{index:03d}", "slot": "customer_highlight", "bucket": index % 8, "gate_source": "fake"}
        for index in range(30)
    ]
    return SimpleNamespace(to_dict=lambda: {
        "source_date": source_date.isoformat(), "camera": camera_id[:8], "selected": selected,
        "gate_stats": {"reused": 0, "assessed": 30, "failed": 0},
    })


def test_preview_returns_30_without_db_or_claude_write(capsys):
    source_date = source_nights()[0]
    started_at = bucket_plans(source_date)[0].start
    sb = FakeSB({"motion_clips": [{
        "id": "clip", "camera_id": "camera-a", "started_at": started_at.isoformat(),
        "duration_sec": 30, "r2_key": "clip.mp4",
    }]})
    calls = []

    def prepare(_sb, source_date, camera_id, *, persist):
        calls.append((source_date, camera_id, persist))
        return _preview_wave(source_date, camera_id)

    locks = []
    assert main(
        ["--source-date", "2026-07-07"], sb=sb, prepare_fn=prepare,
        acquire_lock_fn=lambda: locks.append("acquired") or object(),
        release_lock_fn=lambda _lock: locks.append("released"),
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["selected"]) == 30
    assert calls == [(source_nights()[0], "camera-a", False)]
    assert sb.store.get("clip_vlm_selector_runs", []) == []
    assert sb.store.get("clip_vlm_jobs", []) == []
    assert BACKFILL_SELECTOR_VERSION not in json.dumps(sb.store)
    assert locks == ["acquired", "released"]


def test_preview_rejects_dates_outside_frozen_allowlist():
    with pytest.raises(SystemExit):
        main(["--source-date", "2026-07-15"], sb=FakeSB())


def test_preview_fails_closed_when_activity_worker_owns_gate_lock():
    source_date = source_nights()[0]
    started_at = bucket_plans(source_date)[0].start
    sb = FakeSB({"motion_clips": [{
        "id": "clip", "camera_id": "camera-a", "started_at": started_at.isoformat(),
        "duration_sec": 30, "r2_key": "clip.mp4",
    }]})
    with pytest.raises(RuntimeError, match="activity worker busy"):
        main(["--source-date", "2026-07-07"], sb=sb, acquire_lock_fn=lambda: None)
