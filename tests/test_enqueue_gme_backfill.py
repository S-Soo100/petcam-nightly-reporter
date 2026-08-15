from datetime import datetime, timezone

import pytest

from scripts.enqueue_gme_backfill import (
    BACKFILL_START,
    MAX_BACKFILL_LIMIT,
    _load_motion_page,
    enqueue,
    enqueue_batches,
    is_eligible_metadata,
)


V25_IDENTITY = "d4654168af21d26697ab1bd9a5dc4a05bd92baf5c9328800915cc347803d05b6"


def _row(**changes):
    row = {
        "id": "clip-1", "camera_id": "cam-a", "started_at": "2026-07-15T00:00:00+00:00",
        "r2_key": "terra-clips/clips/a.mp4", "clip_purpose": "production",
    }
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
    assert is_eligible_metadata(_row(clip_purpose="test"), exclusion_state=None, cleanup_state=None) is False


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
    selected_columns = next(value for name, value in query.calls if name == "select")
    assert "clip_purpose" in selected_columns.split(",")


def test_backfill_limit_covers_current_full_corpus():
    assert MAX_BACKFILL_LIMIT == 50_000


class _RpcResult:
    def __init__(self, value):
        self.data = value

    def execute(self):
        return self


class _RpcClient:
    def __init__(self):
        self.calls = []

    def rpc(self, name, args):
        self.calls.append((name, args))
        return _RpcResult(len(args["p_clip_ids"]))


def test_backfill_enqueues_each_keyset_page_as_bounded_rpc():
    client = _RpcClient()
    selected, enqueued = enqueue_batches(
        client,
        [[_row(id="clip-1"), _row(id="clip-2")], [_row(id="clip-3")]],
        apply=True,
        detector_identity=V25_IDENTITY,
    )
    assert (selected, enqueued) == (3, 3)
    assert [len(args["p_clip_ids"]) for _, args in client.calls] == [2, 1]
    assert {args["p_detector_identity"] for _, args in client.calls} == {V25_IDENTITY}


def test_enqueue_rejects_non_sha_detector_identity_before_rpc():
    client = _RpcClient()
    with pytest.raises(ValueError, match="detector identity"):
        enqueue(
            client, ["clip-1"], source="smoke", priority=90, apply=True,
            detector_identity="v2.5",
        )
    assert client.calls == []
