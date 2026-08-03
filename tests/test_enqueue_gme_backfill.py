from datetime import datetime, timezone

from scripts.enqueue_gme_backfill import BACKFILL_START, is_eligible_metadata


def _row(**changes):
    row = {"id": "clip-1", "camera_id": "cam-a", "started_at": "2026-07-15T00:00:00+00:00", "r2_key": "terra-clips/clips/a.mp4"}
    row.update(changes)
    return row


def test_backfill_start_is_kst_july_15_exactly():
    assert BACKFILL_START == datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)


def test_metadata_excludes_quarantine_deleted_missing_and_blank_source():
    assert is_eligible_metadata(_row(), exclusion_state=None, cleanup_state=None) is True
    for state in ("quarantined", "media_deleted"):
        assert is_eligible_metadata(_row(), exclusion_state=state, cleanup_state=None) is False
    for state in ("quarantined", "media_deleted", "source_missing"):
        assert is_eligible_metadata(_row(), exclusion_state=None, cleanup_state=state) is False
    assert is_eligible_metadata(_row(r2_key=None), exclusion_state=None, cleanup_state=None) is False
