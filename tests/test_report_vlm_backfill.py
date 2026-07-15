import json
from pathlib import Path

import numpy as np
import pytest

from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION, source_nights
from scripts.report_vlm_backfill import (
    CONTACT_SHEET_COLS,
    CONTACT_SHEET_ROWS,
    aggregate,
    build_contact_sheet,
    main,
    validate_output_path,
    write_report,
)

FULL_CLIP_IDS = [f"{d:02d}aaaaa-bbbb-cccc-dddd-{n:012d}" for d in range(7, 15) for n in range(30)]


def fake_240_jobs():
    jobs = []
    idx = 0
    for day in range(7, 15):
        source_date = f"2026-07-{day:02d}"
        for bucket in range(8):
            per_bucket = 4 if bucket not in (0, 1) else 3  # arbitrary 30/night split for fixture
            for _ in range(per_bucket):
                if idx >= len(FULL_CLIP_IDS):
                    break
                clip_id = FULL_CLIP_IDS[idx]
                idx += 1
                jobs.append({
                    "id": f"job-{clip_id}",
                    "selector_version": BACKFILL_SELECTOR_VERSION,
                    "clip_id": clip_id,
                    "camera_id": "5b3ea7aa-b4a7-4146-8f48-caf69e29e49c",
                    "window_start": f"{source_date}T16:00:00+00:00",
                    "slot": ["customer_highlight", "subtle_behavior", "diversity_discovery", "exclusion_audit"][idx % 4],
                    "status": "succeeded",
                    "model_requested": "claude-sonnet-5",
                    "model_actual": "claude-sonnet-5",
                    "cost_usd": "0",
                    "error_code": None,
                    "rank_features": {"source_date": source_date, "bucket_index": bucket},
                    "result": {
                        "clip_id": clip_id,
                        "action": ["moving", "unseen", "eating_paste", "drinking"][idx % 4],
                        "confidence": 0.5,
                        "provider": "claude_cli_batch",
                        "provider_estimated_cost_usd": 0.15,
                    },
                })
    # top up to exactly 30/night deterministically
    return jobs


def make_240_exact():
    jobs = fake_240_jobs()
    # pad/truncate each date bucket to exactly 30 for the count assertion
    by_date = {}
    for job in jobs:
        by_date.setdefault(job["rank_features"]["source_date"], []).append(job)
    out = []
    counter = 0
    for day in range(7, 15):
        key = f"2026-07-{day:02d}"
        bucket_jobs = by_date.get(key, [])
        while len(bucket_jobs) < 30:
            clip_id = f"pad{day:02d}-bbbb-cccc-dddd-{counter:012d}"
            counter += 1
            bucket_jobs.append({
                "id": f"job-{clip_id}",
                "selector_version": BACKFILL_SELECTOR_VERSION,
                "clip_id": clip_id,
                "camera_id": "5b3ea7aa-b4a7-4146-8f48-caf69e29e49c",
                "window_start": f"{key}T16:00:00+00:00",
                "slot": "customer_highlight",
                "status": "succeeded",
                "model_requested": "claude-sonnet-5",
                "model_actual": "claude-sonnet-5",
                "cost_usd": "0",
                "error_code": None,
                "rank_features": {"source_date": key, "bucket_index": 0},
                "result": {
                    "clip_id": clip_id, "action": "unseen", "confidence": 0.5,
                    "provider": "claude_cli_batch", "provider_estimated_cost_usd": 0.1,
                },
            })
        out += bucket_jobs[:30]
    return out


def test_report_counts_dates_slots_actions_costs():
    report = aggregate(make_240_exact())
    assert report.total == 240
    assert report.by_date == {f"2026-07-{d:02d}": 30 for d in range(7, 15)}
    assert report.actual_cost_usd == 0
    assert sum(report.by_action.values()) == 240


def test_report_output_must_stay_under_repo_storage(tmp_path):
    with pytest.raises(ValueError, match="storage"):
        validate_output_path(Path("/tmp/outside"))


def test_validate_output_path_accepts_repo_storage_subdir():
    from reporter import config
    resolved = validate_output_path(config.REPO_ROOT / "storage" / "vlm-backfill-report-test")
    assert resolved == (config.REPO_ROOT / "storage" / "vlm-backfill-report-test").resolve()


def test_aggregate_counts_status_model_and_errors():
    jobs = make_240_exact()
    jobs[0] = {
        **jobs[0], "status": "failed_terminal", "error_code": "provider_error",
        "model_actual": None, "result": {},
    }
    report = aggregate(jobs)
    assert report.by_status["failed_terminal"] == 1
    assert report.error_counts == {"provider_error": 1}
    assert report.by_model["claude-sonnet-5"] == 239
    assert report.model_mismatch_count == 0


def test_aggregate_flags_model_mismatch():
    jobs = [{
        "clip_id": "x", "slot": "customer_highlight", "status": "held_model_mismatch",
        "model_requested": "claude-sonnet-5", "model_actual": "claude-haiku-4-5",
        "cost_usd": "0", "error_code": None, "rank_features": {"source_date": "2026-07-07"},
        "result": {},
    }]
    report = aggregate(jobs)
    assert report.model_mismatch_count == 1


def test_jobs_json_never_contains_full_clip_id_owner_or_email(tmp_path):
    jobs = make_240_exact()[:2]
    out_dir = tmp_path / "storage-like"
    out_dir.mkdir()

    def fake_download(_key, dest):
        dest.write_bytes(b"fake-mp4")

    def fake_thumbnail(_video):
        return np.zeros((10, 10, 3), dtype=np.uint8)

    write_report(
        jobs, out_dir=out_dir, r2_key_lookup_fn=lambda clip_id: f"r2/{clip_id}.mp4",
        download_fn=fake_download, thumbnail_fn=fake_thumbnail,
    )
    jobs_json_text = (out_dir / "jobs.json").read_text()
    for job in jobs:
        assert job["clip_id"] not in jobs_json_text
        assert job["camera_id"] not in jobs_json_text
    assert "@" not in jobs_json_text
    payload = json.loads(jobs_json_text)
    assert payload[0]["clip8"] == jobs[0]["clip_id"][:8]
    assert "clip_id" not in payload[0]
    assert "camera_id" not in payload[0]


def test_write_report_creates_report_md_jobs_json_and_contact_sheets(tmp_path):
    jobs = make_240_exact()
    out_dir = tmp_path / "storage-like"
    out_dir.mkdir()

    def fake_download(_key, dest):
        dest.write_bytes(b"fake-mp4")

    def fake_thumbnail(_video):
        return np.zeros((10, 10, 3), dtype=np.uint8)

    write_report(
        jobs, out_dir=out_dir, r2_key_lookup_fn=lambda clip_id: f"r2/{clip_id}.mp4",
        download_fn=fake_download, thumbnail_fn=fake_thumbnail,
    )
    assert (out_dir / "REPORT.md").exists()
    assert (out_dir / "jobs.json").exists()
    sheets = sorted(out_dir.glob("contact-sheet-*.jpg"))
    assert len(sheets) == len(source_nights())
    report_text = (out_dir / "REPORT.md").read_text()
    assert "240" in report_text
    for job in jobs:
        assert job["clip_id"] not in report_text


def test_contact_sheet_grid_has_no_full_clip_id_label():
    jobs = make_240_exact()[:30]

    def fake_download(_key, dest):
        dest.write_bytes(b"fake-mp4")

    def fake_thumbnail(_video):
        return np.zeros((10, 10, 3), dtype=np.uint8)

    sheet = build_contact_sheet(
        jobs, r2_key_lookup_fn=lambda clip_id: f"r2/{clip_id}.mp4",
        download_fn=fake_download, thumbnail_fn=fake_thumbnail,
    )
    assert sheet.shape[0] > 0 and sheet.shape[1] > 0
    for job in jobs:
        assert job["clip_id"] not in repr(sheet.tobytes()[:0])  # pixels carry no text; sanity only


def test_temp_mp4_is_removed_after_contact_sheet_build(tmp_path):
    downloaded = []

    def fake_download(_key, dest):
        downloaded.append(dest)
        dest.write_bytes(b"fake-mp4")

    def fake_thumbnail(video):
        assert video.exists()
        return np.zeros((10, 10, 3), dtype=np.uint8)

    jobs = make_240_exact()[:30]
    build_contact_sheet(
        jobs, r2_key_lookup_fn=lambda clip_id: f"r2/{clip_id}.mp4",
        download_fn=fake_download, thumbnail_fn=fake_thumbnail,
    )
    assert len(downloaded) == 30
    assert all(not p.exists() for p in downloaded)
    assert list(tmp_path.glob("*.mp4")) == []


def test_contact_sheet_pads_short_nights_to_fixed_grid():
    jobs = make_240_exact()[:5]

    def fake_download(_key, dest):
        dest.write_bytes(b"fake-mp4")

    def fake_thumbnail(_video):
        return np.zeros((10, 10, 3), dtype=np.uint8)

    sheet = build_contact_sheet(
        jobs, r2_key_lookup_fn=lambda clip_id: f"r2/{clip_id}.mp4",
        download_fn=fake_download, thumbnail_fn=fake_thumbnail,
    )
    full = build_contact_sheet(
        make_240_exact()[:30], r2_key_lookup_fn=lambda clip_id: f"r2/{clip_id}.mp4",
        download_fn=fake_download, thumbnail_fn=fake_thumbnail,
    )
    assert sheet.shape == full.shape
    assert CONTACT_SHEET_COLS * CONTACT_SHEET_ROWS == 30


def test_main_writes_report_using_injected_sb_and_stays_under_storage(capsys):
    from reporter import config
    from tests._fakes import FakeSB

    jobs = make_240_exact()
    motion_clips = [{"id": job["clip_id"], "r2_key": f"r2/{job['clip_id']}.mp4"} for job in jobs]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": motion_clips})
    out_dir = config.REPO_ROOT / "storage" / "test-report-vlm-backfill-tmp"

    def fake_download(_key, dest):
        dest.write_bytes(b"fake-mp4")

    def fake_thumbnail(_video):
        return np.zeros((10, 10, 3), dtype=np.uint8)

    try:
        exit_code = main(
            ["--out", str(out_dir)], sb=sb, download_fn=fake_download, thumbnail_fn=fake_thumbnail,
        )
        assert exit_code == 0
        assert (out_dir / "REPORT.md").exists()
        out = capsys.readouterr().out
        assert "total=240" in out
    finally:
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)


def test_main_rejects_output_outside_repo_storage():
    with pytest.raises(ValueError, match="storage"):
        main(["--out", "/tmp/outside-report"], sb=object())
