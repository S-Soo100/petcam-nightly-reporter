from datetime import datetime, timezone

import pytest
import scripts.enqueue_gme_backfill as backfill

from scripts.enqueue_gme_backfill import (
    BACKFILL_START,
    MAX_BACKFILL_LIMIT,
    _existing_identity_clip_ids,
    _load_motion_page,
    _new_inventory,
    _select_eligible_page,
    enqueue,
    enqueue_batches,
    is_eligible_metadata,
)


V25_IDENTITY = "d4654168af21d26697ab1bd9a5dc4a05bd92baf5c9328800915cc347803d05b6"
V26_IDENTITY = "89e4738a60ebb71900e05e96f5b7262e8b900f5c9bba9b9cb9e34fca36f789b7"


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


class _R2:
    def __init__(self, existing):
        self.existing = set(existing)
        self.heads = []

    def head_object(self, *, Bucket, Key):
        self.heads.append((Bucket, Key))
        if Key not in self.existing:
            raise RuntimeError("missing")


def test_selection_excludes_existing_identity_before_r2_and_counts_aggregate_reasons():
    rows = [
        _row(id="clip-ready", r2_key="ready.mp4"),
        _row(id="clip-existing", r2_key="existing.mp4"),
        _row(id="clip-test", r2_key="test.mp4", clip_purpose="test"),
        _row(id="clip-quarantine", r2_key="quarantine.mp4"),
        _row(id="clip-deleted", r2_key="deleted.mp4"),
        _row(id="clip-cleanup-missing", r2_key="cleanup-missing.mp4"),
        _row(id="clip-r2-missing", r2_key="r2-missing.mp4"),
    ]
    r2_client = _R2({"ready.mp4"})
    stats = _new_inventory()

    selected = _select_eligible_page(
        rows,
        exclusions={"clip-quarantine": "quarantined", "clip-deleted": "media_deleted"},
        cleanup={"clip-cleanup-missing": "source_missing"},
        existing_clip_ids={"clip-existing"},
        r2_client=r2_client,
        bucket="bucket",
        remaining=50,
        stats=stats,
    )

    assert [row["id"] for row in selected] == ["clip-ready"]
    assert stats == {
        "scanned": 7,
        "selected": 1,
        "eligible": 1,
        "excluded_test": 1,
        "excluded_quarantined": 1,
        "excluded_deleted": 1,
        "excluded_source_missing": 2,
        "excluded_existing": 1,
        "excluded_invalid_metadata": 0,
    }
    assert r2_client.heads == [
        ("bucket", "ready.mp4"),
        ("bucket", "r2-missing.mp4"),
    ]


def test_selection_caps_page_at_remaining_without_counting_unscanned_rows():
    rows = [_row(id=f"clip-{index}", r2_key=f"{index}.mp4") for index in range(3)]
    stats = _new_inventory()
    selected = _select_eligible_page(
        rows,
        exclusions={}, cleanup={}, existing_clip_ids={},
        r2_client=_R2({"0.mp4", "1.mp4", "2.mp4"}),
        bucket="bucket", remaining=2, stats=stats,
    )

    assert [row["id"] for row in selected] == ["clip-0", "clip-1"]
    assert stats["scanned"] == 2
    assert stats["selected"] == 2


def test_selection_scan_all_counts_eligible_rows_beyond_selection_limit():
    rows = [_row(id=f"clip-{index}", r2_key=f"{index}.mp4") for index in range(3)]
    stats = _new_inventory()
    selected = _select_eligible_page(
        rows,
        exclusions={}, cleanup={}, existing_clip_ids={},
        r2_client=_R2({"0.mp4", "1.mp4", "2.mp4"}),
        bucket="bucket", remaining=2, stats=stats, scan_all=True,
    )

    assert [row["id"] for row in selected] == ["clip-0", "clip-1"]
    assert stats["scanned"] == 3
    assert stats["eligible"] == 3
    assert stats["selected"] == 2


def test_main_scans_full_inventory_only_for_dry_run(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(backfill, "create_client", lambda *_args: object())
    monkeypatch.setattr(backfill.config, "GME_DETECTOR_IDENTITY", V26_IDENTITY)

    def fake_batches(_sb, *, limit, detector_identity, stats, scan_all):
        calls.append((limit, detector_identity, scan_all))
        return iter(())

    monkeypatch.setattr(backfill, "iter_eligible_batches", fake_batches)

    assert backfill.main(["--limit", "50"]) == 0
    assert backfill.main(["--limit", "50", "--apply"]) == 0
    assert calls == [
        (50, V26_IDENTITY, True),
        (50, V26_IDENTITY, False),
    ]
    output = capsys.readouterr().out.splitlines()
    assert '"inventory_complete": true' in output[0]
    assert '"inventory_complete": false' in output[1]


class _IdentityQuery:
    def __init__(self, table, rows, calls):
        self.table_name = table
        self.rows = rows
        self.calls = calls

    def select(self, columns):
        self.calls.append((self.table_name, "select", columns))
        return self

    def in_(self, column, values):
        self.calls.append((self.table_name, "in", column, tuple(values)))
        return self

    def eq(self, column, value):
        self.calls.append((self.table_name, "eq", column, value))
        return self

    def execute(self):
        return type("Response", (), {"data": self.rows.get(self.table_name, [])})()


class _IdentityClient:
    def __init__(self):
        self.calls = []
        self.rows = {
            "gme_jobs": [{"clip_id": "clip-job"}],
            "gme_runs": [{"clip_id": "clip-run"}, {"clip_id": "clip-job"}],
        }

    def table(self, name):
        return _IdentityQuery(name, self.rows, self.calls)


def test_existing_identity_lookup_unions_jobs_and_runs_with_exact_identity():
    client = _IdentityClient()
    assert _existing_identity_clip_ids(
        client, ["clip-job", "clip-run", "clip-new"], V26_IDENTITY,
    ) == {"clip-job", "clip-run"}
    assert {
        call for call in client.calls if call[1] == "eq"
    } == {
        ("gme_jobs", "eq", "detector_identity", V26_IDENTITY),
        ("gme_runs", "eq", "detector_identity", V26_IDENTITY),
    }


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
