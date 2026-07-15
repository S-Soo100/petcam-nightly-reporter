import json
from pathlib import Path

from reporter.labeling_triage_models import LabelingTriageClip
from reporter.preview_labeling_triage import select_preview_candidates, write_preview_artifacts


def _clip(n: int, camera="cam-a", hour=0):
    return LabelingTriageClip(
        id=f"00000000-0000-0000-0000-{n:012d}",
        camera_id=camera,
        started_at=f"2026-07-{14 + n % 2:02d}T{hour:02d}:00:00+00:00",
        duration_sec=30.0,
        r2_key=f"secret/{n}.mp4",
    )


def test_selection_round_robins_camera_day_and_six_hour_strata():
    clips = [
        *[_clip(i, "cam-a", 1) for i in range(1, 7)],
        *[_clip(i + 10, "cam-b", 8) for i in range(1, 4)],
        *[_clip(i + 20, "cam-a", 19) for i in range(1, 4)],
    ]
    selected = select_preview_candidates(clips, 6)
    assert len(selected) == 6
    assert len({c.id for c in selected}) == 6
    strata = {(c.camera_id, c.started_at[:10], int(c.started_at[11:13]) // 6) for c in selected}
    assert len(strata) >= 3


def test_artifacts_are_reviewable_without_secrets_or_absolute_paths(tmp_path: Path):
    rows = [{
        "clip8": "00000000",
        "captured_at": "2026-07-15T01:00:00+00:00",
        "camera_id": "cam-a",
        "suggested_route": "quarantine",
        "suggestion_reason": "gate_absent",
        "display_reason": "게코가 감지되지 않음",
        "evidence_identity": "a" * 64,
        "review_file": "review/00000000.mp4",
        "owner_review": "",
    }]
    write_preview_artifacts(tmp_path, rows, {"queried": 1, "assessed": 1})
    assert (tmp_path / "preview.json").exists()
    assert (tmp_path / "preview.csv").exists()
    assert (tmp_path / "REPORT.md").exists()
    assert (tmp_path / "OWNER-REVIEW.md").exists()
    owner_review = (tmp_path / "OWNER-REVIEW.md").read_text()
    assert "review/00000000.mp4" in owner_review
    assert "시스템 제안" not in owner_review
    assert "quarantine" not in owner_review
    assert "gate_absent" not in owner_review
    assert "게코가 감지되지 않음" not in owner_review
    assert "evidence_identity" not in owner_review
    combined = "\n".join(p.read_text() for p in (
        tmp_path / "preview.json", tmp_path / "preview.csv", tmp_path / "REPORT.md"
    ))
    assert json.loads((tmp_path / "preview.json").read_text())[0]["clip8"] == "00000000"
    for forbidden in ("SUPABASE_SERVICE_ROLE_KEY", "secret/", "/Users/", "signed_url", "owner@example"):
        assert forbidden not in combined
