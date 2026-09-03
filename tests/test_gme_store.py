from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reporter.gme_store import (
    GMEJob,
    GMEStoreError,
    StaleGMEJobError,
    claim_jobs,
    complete_job,
    fail_job,
    insert_run,
    operational_stats,
)


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)
V26_IDENTITY = "deccfc8315d3c00edb5bf59db3c573dca568e9d6d7a5da8d7dc93d2082bdb899"
ALGORITHM_VERSION = "gme-motion-v1"


class Result:
    def __init__(self, data):
        self.data = data


class RPC:
    def __init__(self, owner, name, args):
        self.owner, self.name, self.args = owner, name, args

    def execute(self):
        self.owner.calls.append((self.name, self.args))
        value = self.owner.results[self.name]
        if isinstance(value, Exception):
            raise value
        return Result(value)


class SB:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def rpc(self, name, args):
        return RPC(self, name, args)


def _row(**changes):
    row = {
        "id": "job-1", "clip_id": "clip-1", "source": "live", "priority": 100,
        "engine_schema_version": "gme-shadow-v1", "algorithm_version": ALGORITHM_VERSION,
        "detector_identity": "a" * 64, "status": "processing", "attempt_count": 1,
    }
    row.update(changes)
    return row


def test_claim_uses_identity_isolated_rpc_and_frozen_job():
    sb = SB({"fn_claim_gme_jobs_for_contract": [_row()]})
    jobs = claim_jobs(
        sb, limit=3, worker_host="host", now=NOW, include_historical=False,
        detector_identity=V26_IDENTITY, algorithm_version=ALGORITHM_VERSION,
        engine_schema_version="gme-shadow-v1",
    )
    assert jobs == [GMEJob.from_row(_row())]
    assert sb.calls == [("fn_claim_gme_jobs_for_contract", {
        "p_limit": 3,
        "p_worker_host": "host",
        "p_now": NOW.isoformat(),
        "p_include_historical": False,
        "p_detector_identity": V26_IDENTITY,
        "p_algorithm_version": ALGORITHM_VERSION,
        "p_engine_schema_version": "gme-shadow-v1",
    })]
    with pytest.raises(Exception):
        jobs[0].source = "historical"


def test_claim_rejects_invalid_identity_before_rpc():
    sb = SB({})
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        claim_jobs(
            sb, limit=1, worker_host="host", now=NOW, include_historical=True,
            detector_identity="v2.6", algorithm_version=ALGORITHM_VERSION,
            engine_schema_version="gme-shadow-v1",
        )
    assert sb.calls == []


def test_operational_stats_are_isolated_to_the_active_contract():
    expected = {"oldest_live_age_sec": 0, "queued_live": 0, "queued_historical": 7}
    sb = SB({"fn_gme_operational_stats_for_contract": expected})

    actual = operational_stats(
        sb,
        now=NOW,
        detector_identity=V26_IDENTITY,
        algorithm_version=ALGORITHM_VERSION,
        engine_schema_version="gme-shadow-v1",
    )

    assert actual == expected
    assert sb.calls == [("fn_gme_operational_stats_for_contract", {
        "p_now": NOW.isoformat(),
        "p_detector_identity": V26_IDENTITY,
        "p_algorithm_version": ALGORITHM_VERSION,
        "p_engine_schema_version": "gme-shadow-v1",
    })]


def test_job_rejects_unknown_source_or_status_before_processing():
    with pytest.raises(GMEStoreError):
        GMEJob.from_row(_row(source="other"))
    with pytest.raises(GMEStoreError):
        GMEJob.from_row(_row(status="other"))


def test_complete_rejects_stale_lease():
    with pytest.raises(StaleGMEJobError):
        complete_job(SB({"fn_complete_gme_job": False}), job_id="j", run_id="r", worker_host="h")


def test_fail_rejects_non_allowlisted_code_before_rpc():
    sb = SB({"fn_fail_gme_job": True})
    with pytest.raises(ValueError):
        fail_job(sb, job_id="j", failure_code="raw secret error", retryable=True, worker_host="h", now=NOW)
    assert sb.calls == []


def test_rpc_errors_are_redacted():
    sb = SB({"fn_claim_gme_jobs_for_contract": RuntimeError("https://secret.invalid?key=secret")})
    with pytest.raises(GMEStoreError) as error:
        claim_jobs(
            sb, limit=1, worker_host="h", now=NOW, include_historical=True,
            detector_identity=V26_IDENTITY, algorithm_version=ALGORITHM_VERSION,
            engine_schema_version="gme-shadow-v1",
        )
    assert "secret.invalid" not in str(error.value)


def test_insert_rejects_malformed_or_oversized_state_intervals_before_rpc():
    sb = SB({"fn_insert_gme_run": {"id": "run"}})
    with pytest.raises(GMEStoreError):
        insert_run(sb, {"state_intervals": {}, "tracking_quality": {}})
    with pytest.raises(GMEStoreError):
        insert_run(sb, {"state_intervals": [{}] * 10001, "tracking_quality": {}})
    assert sb.calls == []
