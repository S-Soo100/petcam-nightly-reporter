import pytest

from scripts.enqueue_gme_smoke import (
    select_recovery_smoke,
    select_smoke,
    validate_recovery_incident,
)


V26_IDENTITY = "89e4738a60ebb71900e05e96f5b7262e8b900f5c9bba9b9cb9e34fca36f789b7"


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
