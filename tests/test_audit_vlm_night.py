from datetime import datetime, timezone

from tests._fakes import FakeSB
from reporter.audit_vlm_night import audit_night
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION

# 07-15 밤 4개 window_start(UTC): KST 20/22/00/02 → UTC 11/13/15/17
_W = ["2026-07-15T11:00:00+00:00", "2026-07-15T13:00:00+00:00",
      "2026-07-15T15:00:00+00:00", "2026-07-15T17:00:00+00:00"]
_NOW = datetime(2026, 7, 16, 10, tzinfo=timezone.utc)


def _job(window_start, camera="camA", *, selector="budget-router-v1", status="succeeded",
         host="mac-mini", model="claude-sonnet-5", run_id="20260715T220000", queued_at=None, cost="0"):
    return {"selector_version": selector, "window_start": window_start, "camera_id": camera, "status": status,
            "producer_host": host, "producer_run_id": run_id,
            "model_actual": model if status == "succeeded" else None,
            "queued_at": queued_at or window_start, "cost_usd": cost}


def _clean():
    return FakeSB({"clip_vlm_jobs": [_job(w, camera=f"cam{i}") for i, w in enumerate(_W)]})


def test_clean_night_passes():
    result = audit_night(_clean(), "2026-07-15", now=_NOW, expected_host="mac-mini")
    assert result["all_pass"] is True and result["violations"] == []
    assert result["missing_windows"] == []
    assert result["slack_delivery"] == "not_verifiable_from_db"


def test_missing_window_is_violation():
    sb = FakeSB({"clip_vlm_jobs": [_job(w) for w in _W[:3]]})
    result = audit_night(sb, "2026-07-15", now=_NOW, expected_host="mac-mini")
    assert "missing_windows" in result["violations"]
    assert len(result["missing_windows"]) == 1  # 4번째 window(02:00 KST) 누락
    assert result["all_pass"] is False


def test_wrong_producer_host_is_violation():
    sb = _clean()
    sb.store["clip_vlm_jobs"][0]["producer_host"] = "macbook.local"
    result = audit_night(sb, "2026-07-15", now=_NOW, expected_host="mac-mini")
    assert "host_mismatch" in result["violations"]


def test_over_camera_window_cap_is_violation():
    sb = FakeSB({"clip_vlm_jobs": [_job(_W[0], camera="camA") for _ in range(5)]
                                   + [_job(w) for w in _W[1:]]})
    result = audit_night(sb, "2026-07-15", now=_NOW, expected_host="mac-mini")
    assert "over_camera_window_cap" in result["violations"]


def test_stale_open_job_is_violation():
    sb = _clean()
    sb.store["clip_vlm_jobs"].append(_job(_W[3], camera="camZ", status="failed_retryable",
                                          queued_at="2026-07-15T17:00:00+00:00"))
    now = datetime(2026, 7, 15, 18, tzinfo=timezone.utc)  # 60분 경과
    result = audit_night(sb, "2026-07-15", now=now, expected_host="mac-mini")
    assert result["stale_open_over_30m"] >= 1 and "stale_open_over_30m" in result["violations"]


def test_model_mismatch_is_violation():
    sb = _clean()
    sb.store["clip_vlm_jobs"][0]["model_actual"] = "claude-sonnet-4-6"
    result = audit_night(sb, "2026-07-15", now=_NOW, expected_host="mac-mini")
    assert result["model_mismatch"] >= 1 and "model_mismatch" in result["violations"]


def test_selector_crossover_is_violation():
    sb = _clean()
    sb.store["clip_vlm_jobs"][0]["producer_run_id"] = "backfill-2026-07-07-0"  # 정규 job 이 backfill 산출물
    result = audit_night(sb, "2026-07-15", now=_NOW, expected_host="mac-mini")
    assert result["selector_crossover"] >= 1 and "selector_crossover" in result["violations"]


def test_backfill_jobs_excluded_from_regular_checks():
    sb = _clean()
    sb.store["clip_vlm_jobs"].append(_job(_W[0], camera="camA", selector=BACKFILL_SELECTOR_VERSION,
                                          run_id="backfill-2026-07-07-0"))
    result = audit_night(sb, "2026-07-15", now=_NOW, expected_host="mac-mini")
    assert result["all_pass"] is True  # backfill 은 정규 cap/crossover 에 포함 안 됨
    assert result["backfill_jobs_in_night"] == 1
