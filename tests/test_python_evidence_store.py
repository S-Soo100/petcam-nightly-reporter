"""python_evidence_store — durable queue RPC client (claim/complete/fail/insert_run).

DB 무의존: sb.rpc(name,args) 를 기록/응답하는 recording fake 로 RPC 이름·인자·파싱·멱등·
allowlist·에러 위생을 고정한다(donts/python#13). raw Supabase 에러가 응답/로그로 새지 않는지도 검증.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reporter import python_evidence_store as store
from gecko_vision_gate.temporal_evidence import TemporalEvidence, TemporalPoint

NOW = datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc)


class _RpcResult:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class RecSB:
    """rpc(name,args) 를 기록하고 canned 응답을 돌려주는 fake. raise_on 으로 DB 에러 재현."""

    def __init__(self, responses=None, raise_on=None):
        self.calls = []  # (name, args)
        self.responses = responses or {}
        self.raise_on = raise_on or set()

    def rpc(self, name, args):
        self.calls.append((name, args))
        if name in self.raise_on:
            raise RuntimeError("postgrest: password=hunter2 secret leak https://x.supabase.co")
        return _RpcResult(self.responses.get(name))


def _job_row(**over):
    base = {
        "id": "job-1", "clip_id": "clip-1", "source": "live", "status": "processing",
        "priority": 100, "evidence_schema_version": "python-evidence-raw-v1",
        "algorithm_version": "croi-temporal-v1", "attempt_count": 1,
    }
    base.update(over)
    return base


def _temporal(level1="ok"):
    return TemporalEvidence(
        evidence_schema_version="python-evidence-raw-v1",
        algorithm_version="croi-temporal-v1",
        level0_status="ok", level1_status=level1,
        decoded_frame_count=10, point_stride=1,
        global_motion_series=(TemporalPoint(0.0, 1.0), TemporalPoint(0.1, 2.0)),
        roi_motion_series=(TemporalPoint(0.0, 3.0),) if level1 == "ok" else (),
        motion_summary={"global_max": 2.0}, spatial_dwell={"grid_size": 4},
        periodicity_summary={"n_points": 2}, motion_excursions=(),
    )


def _prelabel(**over):
    base = {
        "id": "pre-1", "model_name": "rf-detr-nano", "model_version": "gecko_v2",
        "checkpoint_sha256": "abc", "threshold": 0.25, "sampler_version": "even-uniform-v1",
        "schema_version": "gate-evidence-v1", "frames_sampled": 12,
    }
    base.update(over)
    return base


def _producer():
    return store.ProducerInfo(host="mac-mini", run_id="20260717T030000", code_ref="deadbeef")


# ── claim ──

def test_claim_jobs_calls_rpc_with_args_and_parses():
    sb = RecSB({"fn_claim_python_evidence_jobs": [_job_row(), _job_row(id="job-2")]})
    jobs = store.claim_jobs(sb, limit=30, worker_host="mac-mini", now=NOW)
    name, args = sb.calls[0]
    assert name == "fn_claim_python_evidence_jobs"
    assert args == {"p_limit": 30, "p_worker_host": "mac-mini", "p_now": NOW.isoformat()}
    assert [j.id for j in jobs] == ["job-1", "job-2"]
    assert jobs[0].clip_id == "clip-1" and jobs[0].source == "live"


def test_claim_jobs_empty():
    sb = RecSB({"fn_claim_python_evidence_jobs": []})
    assert store.claim_jobs(sb, limit=30, worker_host="h", now=NOW) == []


def test_claim_jobs_none_data():
    sb = RecSB({"fn_claim_python_evidence_jobs": None})
    assert store.claim_jobs(sb, limit=30, worker_host="h", now=NOW) == []


def test_claim_rejects_unknown_status_or_source():
    sb = RecSB({"fn_claim_python_evidence_jobs": [_job_row(status="weird")]})
    with pytest.raises(store.EvidenceStoreError):
        store.claim_jobs(sb, limit=30, worker_host="h", now=NOW)
    sb2 = RecSB({"fn_claim_python_evidence_jobs": [_job_row(source="pirate")]})
    with pytest.raises(store.EvidenceStoreError):
        store.claim_jobs(sb2, limit=30, worker_host="h", now=NOW)


# ── complete ──

def test_complete_job_calls_rpc():
    sb = RecSB({"fn_complete_python_evidence_job": True})
    store.complete_job(sb, job_id="job-1", run_id="run-1", worker_host="mac-mini")
    name, args = sb.calls[0]
    assert name == "fn_complete_python_evidence_job"
    assert args == {"p_job_id": "job-1", "p_run_id": "run-1", "p_worker_host": "mac-mini"}


def test_complete_job_stale_raises():
    sb = RecSB({"fn_complete_python_evidence_job": False})  # lease 불일치 등 stale
    with pytest.raises(store.StaleJobError):
        store.complete_job(sb, job_id="job-1", run_id="run-1", worker_host="mac-mini")


# ── fail ──

def test_fail_job_allowlist_and_args():
    sb = RecSB({"fn_fail_python_evidence_job": True})
    store.fail_job(sb, job_id="job-1", failure_code="r2_download_failed",
                   retryable=True, worker_host="mac-mini", now=NOW)
    name, args = sb.calls[0]
    assert name == "fn_fail_python_evidence_job"
    assert args["p_failure_code"] == "r2_download_failed"
    assert args["p_retryable"] is True
    assert args["p_now"] == NOW.isoformat()


def test_fail_job_rejects_unknown_failure_code_locally():
    sb = RecSB({"fn_fail_python_evidence_job": True})
    with pytest.raises(ValueError):
        store.fail_job(sb, job_id="job-1", failure_code="made_up_code",
                       retryable=False, worker_host="h", now=NOW)
    assert sb.calls == []  # RPC 도달 전에 로컬 거부


# ── insert_run ──

def test_insert_run_builds_payload_and_serializes_series():
    sb = RecSB({"fn_insert_python_evidence_run": {"id": "run-1", "clip_id": "clip-1"}})
    job = store.claim_jobs(
        RecSB({"fn_claim_python_evidence_jobs": [_job_row()]}),
        limit=1, worker_host="h", now=NOW,
    )[0]
    out = store.insert_run(sb, job=job, temporal=_temporal(), prelabel=_prelabel(), producer=_producer())
    assert out == {"id": "run-1", "clip_id": "clip-1"}
    name, args = sb.calls[0]
    assert name == "fn_insert_python_evidence_run"
    p = args["p_run"]
    assert p["clip_id"] == "clip-1" and p["job_id"] == "job-1"
    assert p["level0_status"] == "ok" and p["level1_status"] == "ok"
    # 시계열이 JSON 배열로 직렬화됨 (jsonb_array_length 계약)
    assert isinstance(p["global_motion_series"], list) and len(p["global_motion_series"]) == 2
    assert isinstance(p["roi_motion_series"], list) and len(p["roi_motion_series"]) == 1
    # prelabel provenance 7컬럼 전달
    assert p["model_version"] == "gecko_v2" and p["prelabel_id"] == "pre-1"
    # identity 는 canonical hash (non-null)
    assert p["source_prelabel_identity"] and p["source_prelabel_identity"] != "none"


def test_insert_run_no_prelabel_identity_none():
    sb = RecSB({"fn_insert_python_evidence_run": {"id": "run-2"}})
    job = store.claim_jobs(
        RecSB({"fn_claim_python_evidence_jobs": [_job_row()]}), limit=1, worker_host="h", now=NOW,
    )[0]
    store.insert_run(sb, job=job, temporal=_temporal(level1="no_bbox"), prelabel=None, producer=_producer())
    p = sb.calls[0][1]["p_run"]
    assert p["source_prelabel_identity"] == "none"
    assert p["prelabel_id"] is None
    assert p["roi_motion_series"] == []


def test_insert_run_idempotent_returns_existing():
    # 동일 identity 재삽입: DB 가 기존 run 반환 → 그대로 통과(변경 없음)
    sb = RecSB({"fn_insert_python_evidence_run": {"id": "existing-run", "clip_id": "clip-1"}})
    job = store.claim_jobs(
        RecSB({"fn_claim_python_evidence_jobs": [_job_row()]}), limit=1, worker_host="h", now=NOW,
    )[0]
    out = store.insert_run(sb, job=job, temporal=_temporal(), prelabel=_prelabel(), producer=_producer())
    assert out["id"] == "existing-run"


def test_identity_stable_for_same_prelabel():
    a = store.source_prelabel_identity(_prelabel())
    b = store.source_prelabel_identity(_prelabel())
    c = store.source_prelabel_identity(_prelabel(threshold=0.10))
    assert a == b and a != c and a != "none"
    assert store.source_prelabel_identity(None) == "none"


# ── error hygiene ──

def test_db_error_is_mapped_and_not_leaked():
    sb = RecSB(raise_on={"fn_claim_python_evidence_jobs"})
    with pytest.raises(store.EvidenceStoreError) as ei:
        store.claim_jobs(sb, limit=30, worker_host="h", now=NOW)
    msg = str(ei.value)
    assert "hunter2" not in msg and "password" not in msg and "supabase.co" not in msg


# ── H2: store_prelabel(clip_prelabels 멱등) + prelabel_result_from_row ──

class _TableSB:
    """clip_prelabels upsert 를 기록하는 fake."""

    def __init__(self):
        self.prelabels = []

    def table(self, name):
        assert name == "clip_prelabels"
        return self

    def upsert(self, row, on_conflict=None):
        self._pending = (row, on_conflict)
        return self

    def execute(self):
        row = dict(self._pending[0]); row.setdefault("id", f"pre-{len(self.prelabels)}")
        self.prelabels.append((row, self._pending[1]))
        return _RpcResult([row])


def _gate_provenance():
    from gecko_vision_gate.provenance import GateProvenance
    return GateProvenance(model_name="rf-detr-nano", model_version="gecko_v2", checkpoint_sha256="abc",
                          threshold=0.10, sampler_version="even-uniform-v1", schema_version="gate-evidence-v1",
                          frames_sampled=12)


def _prelabel_result_obj():
    from gecko_vision_gate.schema import PrelabelResult
    return PrelabelResult(gecko_visible=True, visibility_confidence=0.9, frames_sampled=12,
                          model_name="rf-detr-nano", model_version="gecko_v2", gecko_bbox=[10, 10, 20, 20])


def _motion_obj():
    from gecko_vision_gate.motion_evidence import MotionMetrics
    return MotionMetrics(2, 0.5, 0.1, 0.1, 0.9, 0.0, 0.0, False)


def test_store_prelabel_idempotent_identity_and_row_shape():
    sb = _TableSB()
    row = store.store_prelabel(sb, clip_id="clip-1", result=_prelabel_result_obj(),
                               motion=_motion_obj(), provenance=_gate_provenance(), producer=_producer())
    stored_row, on_conflict = sb.prelabels[0]
    # 7-column identity on_conflict (activity_store 와 동일)
    assert on_conflict == store._PRELABEL_CONFLICT
    assert "model_version" in stored_row and stored_row["threshold"] == 0.10
    assert "motion_metrics" in stored_row  # activity 호환(reconstruct 가능)
    assert "id" in row


def test_prelabel_result_from_row_roundtrip():
    row = {"gecko_visible": True, "visibility_confidence": 0.9, "frames_sampled": 12,
           "model_name": "rf-detr-nano", "model_version": "gecko_v2", "gecko_bbox": [1, 2, 3, 4],
           "detected_objects": [{"type": "gecko", "confidence": 0.9, "bbox": [1, 2, 3, 4], "frame_ts": 0.0}]}
    res = store.prelabel_result_from_row(row)
    assert res.gecko_visible is True and res.gecko_bbox == [1, 2, 3, 4]
    assert res.detected_objects[0].type == "gecko"


# ── H4: JSON 계약 Python 경계 검증 (malformed → RPC 도달 전 거부) ──

def _job_for_insert():
    return store.claim_jobs(RecSB({"fn_claim_python_evidence_jobs": [_job_row()]}),
                            limit=1, worker_host="h", now=NOW)[0]


def test_insert_run_rejects_negative_series_value():
    sb = RecSB({"fn_insert_python_evidence_run": {"id": "run-x"}})
    bad = TemporalEvidence(
        evidence_schema_version="python-evidence-raw-v1", algorithm_version="croi-temporal-v1",
        level0_status="ok", level1_status="no_bbox", decoded_frame_count=3, point_stride=1,
        global_motion_series=(TemporalPoint(0.0, -1.0),), roi_motion_series=(),
        motion_summary={}, spatial_dwell={}, periodicity_summary={}, motion_excursions=(),
    )
    with pytest.raises(store.EvidenceStoreError):
        store.insert_run(sb, job=_job_for_insert(), temporal=bad, prelabel=None, producer=_producer())
    assert sb.calls == []  # RPC 도달 전 거부


def test_insert_run_rejects_oversized_series():
    sb = RecSB({"fn_insert_python_evidence_run": {"id": "run-x"}})
    big = TemporalEvidence(
        evidence_schema_version="python-evidence-raw-v1", algorithm_version="croi-temporal-v1",
        level0_status="ok", level1_status="no_bbox", decoded_frame_count=3, point_stride=1,
        global_motion_series=tuple(TemporalPoint(float(i), 1.0) for i in range(257)),
        roi_motion_series=(), motion_summary={}, spatial_dwell={}, periodicity_summary={}, motion_excursions=(),
    )
    with pytest.raises(store.EvidenceStoreError):
        store.insert_run(sb, job=_job_for_insert(), temporal=big, prelabel=None, producer=_producer())


def test_insert_run_accepts_valid_payload():
    sb = RecSB({"fn_insert_python_evidence_run": {"id": "run-ok"}})
    out = store.insert_run(sb, job=_job_for_insert(), temporal=_temporal(), prelabel=None, producer=_producer())
    assert out == {"id": "run-ok"} and sb.calls  # 정상 payload 는 통과
