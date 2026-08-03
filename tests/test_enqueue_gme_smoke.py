from scripts.enqueue_gme_smoke import select_smoke


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
