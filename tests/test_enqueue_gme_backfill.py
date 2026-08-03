from datetime import datetime, timezone

from scripts.enqueue_gme_backfill import (
    BACKFILL_START,
    MAX_BACKFILL_LIMIT,
    _load_motion_page,
    is_eligible_metadata,
)


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


class _Query:
    def __init__(self):
        self.calls = []

    def table(self, value):
        self.calls.append(("table", value))
        return self

    def select(self, value):
        self.calls.append(("select", value))
        return self

    def gte(self, column, value):
        self.calls.append(("gte", column, value))
        return self

    def gt(self, column, value):
        self.calls.append(("gt", column, value))
        return self

    def order(self, column):
        self.calls.append(("order", column))
        return self

    def limit(self, value):
        self.calls.append(("limit", value))
        return self

    def execute(self):
        return type("Response", (), {"data": []})()


def test_motion_page_uses_id_keyset_after_fixed_start():
    query = _Query()
    _load_motion_page(query, after_id="clip-5000", page_size=500)
    assert ("gte", "started_at", BACKFILL_START.isoformat()) in query.calls
    assert ("gt", "id", "clip-5000") in query.calls
    assert ("order", "id") in query.calls
    assert ("limit", 500) in query.calls


def test_backfill_limit_covers_current_full_corpus():
    assert MAX_BACKFILL_LIMIT == 50_000
