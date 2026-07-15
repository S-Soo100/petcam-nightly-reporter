from datetime import datetime, timezone

from reporter.labeling_triage_indexer import list_labeling_triage_candidates
from tests._fakes import FakeSB


START = datetime(2026, 7, 14, tzinfo=timezone.utc)
END = datetime(2026, 7, 16, tzinfo=timezone.utc)


def _clip(n: int, *, has_motion=True, r2_key="clips/x.mp4"):
    return {
        "id": f"00000000-0000-0000-0000-{n:012d}",
        "camera_id": "10000000-0000-0000-0000-000000000001",
        "started_at": f"2026-07-15T{n:02d}:00:00+00:00",
        "duration_sec": 30.0,
        "r2_key": r2_key,
        "has_motion": has_motion,
    }


def test_pages_past_completed_identity_without_starvation():
    rows = [_clip(i) for i in range(1, 5)]
    sb = FakeSB({
        "camera_clips": rows,
        "clip_labeling_triage": [
            {"clip_id": rows[0]["id"], "owner_decision": None, "evidence_snapshot": {"identity": "same"}},
            {"clip_id": rows[1]["id"], "owner_decision": None, "evidence_snapshot": {"identity": "same"}},
        ],
    })
    got = list_labeling_triage_candidates(
        sb, start=START, end=END, limit=2, page_size=2,
        identity_for_clip=lambda _clip_id: "same",
    )
    assert [c.id for c in got] == [rows[2]["id"], rows[3]["id"]]


def test_excludes_sessions_owner_decisions_and_non_labelable_rows():
    rows = [_clip(i) for i in range(1, 7)]
    rows[4]["has_motion"] = False
    rows[5]["r2_key"] = None
    sb = FakeSB({
        "camera_clips": rows,
        "clip_labeling_sessions": [{"clip_id": rows[0]["id"]}],
        "clip_labeling_triage": [
            {"clip_id": rows[1]["id"], "owner_decision": "label", "evidence_snapshot": {}},
            {"clip_id": rows[2]["id"], "owner_decision": "skip", "evidence_snapshot": {}},
            {"clip_id": rows[3]["id"], "owner_decision": None, "evidence_snapshot": {"identity": "old"}},
        ],
    })
    got = list_labeling_triage_candidates(
        sb, start=START, end=END, limit=10, page_size=2,
        identity_for_clip=lambda _clip_id: "new",
    )
    assert [c.id for c in got] == [rows[3]["id"]]


def test_same_identity_is_done_but_different_identity_is_reassessed():
    rows = [_clip(1), _clip(2)]
    sb = FakeSB({
        "camera_clips": rows,
        "clip_labeling_triage": [
            {"clip_id": rows[0]["id"], "owner_decision": None, "evidence_snapshot": {"identity": "new"}},
            {"clip_id": rows[1]["id"], "owner_decision": None, "evidence_snapshot": {"identity": "old"}},
        ],
    })
    got = list_labeling_triage_candidates(
        sb, start=START, end=END, limit=10, page_size=10,
        identity_for_clip=lambda _clip_id: "new",
    )
    assert [c.id for c in got] == [rows[1]["id"]]
