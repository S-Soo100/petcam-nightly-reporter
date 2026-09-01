import pytest
import scripts.enqueue_gme_smoke as smoke

from scripts.enqueue_gme_smoke import (
    load_existing_identity_jobs,
    select_recovery_smoke,
    select_smoke,
    validate_recovery_incident,
)


V26_IDENTITY = "deccfc8315d3c00edb5bf59db3c573dca568e9d6d7a5da8d7dc93d2082bdb899"


class _CaptureQuery:
    def __init__(self):
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def select(self, columns):
        self.calls.append(("select", columns))
        return self

    def eq(self, column, value):
        self.calls.append(("eq", column, value))
        return self

    def execute(self):
        return type("Response", (), {"data": []})()


def test_existing_identity_lookup_is_scoped_to_smoke_source():
    query = _CaptureQuery()
    assert load_existing_identity_jobs(query, detector_identity=V26_IDENTITY) == []
    assert ("eq", "detector_identity", V26_IDENTITY) in query.calls
    assert ("eq", "source", "smoke") in query.calls


def test_smoke_selects_exactly_ten_and_prefers_camera_nights():
    rows = []
    for camera in ("a", "b"):
        for day in range(1, 7):
            rows.append({"id": f"{camera}{day}", "camera_id": camera, "started_at": f"2026-08-{day:02d}T12:00:00Z"})
    selected = select_smoke(rows, limit=10)
    assert len(selected) == 10
    assert len({r["camera_id"] for r in selected}) == 2
    assert len({(r["camera_id"], r["started_at"][:10]) for r in selected}) == 10


def test_smoke_never_duplicates_clip_ids():
    rows = [
        {"id": "same", "camera_id": "a", "started_at": "2026-08-01T00:00:00Z"},
        {"id": "same", "camera_id": "a", "started_at": "2026-08-01T00:00:00Z"},
    ]
    assert len(select_smoke(rows, limit=10)) == 1


def test_main_passes_active_detector_identity_to_eligible_lookup(monkeypatch):
    rows = [
        {
            "id": f"clip-{index}",
            "camera_id": f"camera-{index % 2}",
            "started_at": f"2026-08-{index + 1:02d}T00:00:00Z",
        }
        for index in range(10)
    ]
    captured = []
    monkeypatch.setattr(smoke, "create_client", lambda *_args: object())
    monkeypatch.setattr(smoke.config, "GME_DETECTOR_IDENTITY", V26_IDENTITY)
    monkeypatch.setattr(smoke, "load_existing_identity_jobs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        smoke,
        "load_eligible",
        lambda _sb, *, limit, detector_identity: (
            captured.append((limit, detector_identity)) or rows
        ),
    )
    monkeypatch.setattr(smoke, "enqueue", lambda *_args, **_kwargs: 0)

    assert smoke.main([]) == 0
    assert captured == [(200, V26_IDENTITY)]


def test_recovery_excludes_every_clip_already_bound_to_identity():
    rows = [
        {"id": f"clip-{i}", "camera_id": f"camera-{i % 2}", "started_at": f"2026-08-{i + 1:02d}T00:00:00Z"}
        for i in range(12)
    ]
    selected = select_recovery_smoke(
        rows, existing_clip_ids={"clip-0", "clip-1"}, limit=10,
    )
    assert len(selected) == 10
    assert not ({row["id"] for row in selected} & {"clip-0", "clip-1"})


def test_recovery_requires_exact_immutable_incident():
    incident = [
        {
            "id": f"job-{i}",
            "clip_id": f"clip-{i}",
            "source": "smoke",
            "status": "failed_terminal",
            "failure_code": "invalid_metadata",
            "result_run_id": None,
            "detector_identity": V26_IDENTITY,
        }
        for i in range(10)
    ]
    assert validate_recovery_incident(incident, detector_identity=V26_IDENTITY) == {
        row["clip_id"] for row in incident
    }

    incident[-1]["status"] = "failed_retryable"
    with pytest.raises(ValueError, match="incident contract"):
        validate_recovery_incident(incident, detector_identity=V26_IDENTITY)


def test_recovery_rejects_duplicate_clip_or_result_run():
    incident = [
        {
            "id": f"job-{i}",
            "clip_id": f"clip-{i}",
            "source": "smoke",
            "status": "failed_terminal",
            "failure_code": "invalid_metadata",
            "result_run_id": None,
            "detector_identity": V26_IDENTITY,
        }
        for i in range(10)
    ]
    incident[-1]["clip_id"] = incident[0]["clip_id"]
    incident[-1]["result_run_id"] = "unexpected"
    with pytest.raises(ValueError, match="incident contract"):
        validate_recovery_incident(incident, detector_identity=V26_IDENTITY)
