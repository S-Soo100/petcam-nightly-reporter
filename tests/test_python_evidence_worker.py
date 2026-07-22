"""python_evidence_worker — 전 영상 evidence 오케스트레이션 call-count/격리/경계 테스트.

activity_worker 테스트와 같은 스타일: IO 경계(claim/download/gate/compute/insert/complete/fail)를
spy 로 주입해 호출 횟수·순서·retry/terminal 매핑을 고정한다(DB/R2/모델 무의존). 금지 동작(selector/
VLM/behavior/app write) 0 도 여기서 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reporter import python_evidence_worker as worker
from reporter import gate_lock
from reporter.gate_runner import InsufficientSampleFrames
from reporter.r2 import R2AccessDenied, R2SourceMissing
from reporter.python_evidence_store import EvidenceStoreError, EvidenceJob, ProducerInfo, StaleJobError
from gecko_vision_gate.temporal_evidence import TemporalEvidence, TemporalPoint
from gecko_vision_gate.schema import PrelabelResult

NOW = datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc)
PRODUCER = ProducerInfo(host="mac-mini", run_id="run1", code_ref="ref")
GATE_CFG = {
    "checkpoint_path": "ckpt", "threshold": 0.25, "num_frames": 12, "model_size": "nano",
    "model_version": "gecko_v2", "checkpoint_sha256": "sha", "sampler_version": "even-uniform-v1",
    "schema_version": "gate-evidence-v1",
}


def _job(clip_id="clip-1", jid=None):
    return EvidenceJob(
        id=jid or f"job-{clip_id}", clip_id=clip_id, source="live", status="processing",
        priority=100, evidence_schema_version="python-evidence-raw-v1",
        algorithm_version="croi-temporal-v1", attempt_count=1,
    )


def _temporal(level0="ok", level1="no_bbox"):
    return TemporalEvidence(
        evidence_schema_version="python-evidence-raw-v1", algorithm_version="croi-temporal-v1",
        level0_status=level0, level1_status=level1, decoded_frame_count=10, point_stride=1,
        global_motion_series=(TemporalPoint(0.0, 1.0),), roi_motion_series=(),
        motion_summary={}, spatial_dwell={}, periodicity_summary={}, motion_excursions=(),
    )


def _prelabel_result():
    return PrelabelResult(
        gecko_visible=True, visibility_confidence=0.9, frames_sampled=12,
        model_name="rf-detr-nano", model_version="gecko_v2", gecko_bbox=[10, 10, 20, 20],
    )


class Spies:
    """주입 가능한 IO 경계 spy 묶음. 기본은 성공 경로."""

    def __init__(self, *, prelabel_row=None, temporal=None, download_raises=False,
                 gate_raises=False, gate_insufficient=False, compute_raises=False,
                 complete_raises=None, insert_raises=False, download_exc=None):
        self.calls = {"download": 0, "find": 0, "gate": 0, "compute": 0, "insert": 0,
                      "complete": 0, "fail": []}
        self.insert_prelabels = []
        self._prelabel_row = prelabel_row
        self._temporal = temporal or _temporal()
        self._download_raises = download_raises
        self._download_exc = download_exc
        self._gate_raises = gate_raises
        self._gate_insufficient = gate_insufficient
        self._compute_raises = compute_raises
        self._complete_raises = complete_raises
        self._insert_raises = insert_raises

    def download(self, r2_key, dest):
        self.calls["download"] += 1
        if self._download_exc is not None:
            raise self._download_exc
        if self._download_raises:
            raise RuntimeError("r2 boom")

    def find_prelabel(self, sb, clip_id, *a):
        self.calls["find"] += 1
        return self._prelabel_row

    def result_from_row(self, row):
        return _prelabel_result()

    def run_gate(self, sb, video_path, clip_id, producer):
        self.calls["gate"] += 1
        if self._gate_insufficient:
            raise InsufficientSampleFrames(found=1, required=6)
        if self._gate_raises:
            raise RuntimeError("detector boom")
        return _prelabel_result(), {"id": f"fresh-pre-{clip_id}"}

    def compute(self, video_path, result):
        self.calls["compute"] += 1
        if self._compute_raises:
            raise RuntimeError("cv boom")
        return self._temporal

    def insert_run(self, sb, *, job, temporal, prelabel, producer):
        self.calls["insert"] += 1
        self.insert_prelabels.append(prelabel)
        if self._insert_raises:
            raise EvidenceStoreError("db down")
        return {"id": f"run-{job.clip_id}"}

    def complete(self, sb, *, job_id, run_id, worker_host):
        self.calls["complete"] += 1
        if self._complete_raises:
            raise self._complete_raises

    def fail(self, sb, *, job_id, failure_code, retryable, worker_host, now):
        self.calls["fail"].append((job_id, failure_code, retryable))


def _run_jobs(jobs, clip_keys, spies):
    return worker.process_jobs(
        sb=object(), jobs=jobs, clip_keys=clip_keys, worker_host="mac-mini", producer=PRODUCER,
        gate_config=GATE_CFG, now=NOW, download_fn=spies.download, find_prelabel_fn=spies.find_prelabel,
        result_from_row_fn=spies.result_from_row, run_gate_fn=spies.run_gate, compute_fn=spies.compute,
        insert_run_fn=spies.insert_run, complete_fn=spies.complete, fail_fn=spies.fail,
    )


# ── existing prelabel → detector 0 ──

def test_existing_prelabel_reuses_no_detector():
    s = Spies(prelabel_row={"id": "pre-1"}, temporal=_temporal(level1="ok"))
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["download"] == 1
    assert s.calls["gate"] == 0  # detector 재호출 금지
    assert s.calls["compute"] == 1 and s.calls["insert"] == 1 and s.calls["complete"] == 1
    assert stats["reused"] == 1 and stats["ok"] == 0 and stats["failed"] == 0


# ── missing prelabel → detector once ──

def test_missing_prelabel_runs_detector_once():
    s = Spies(prelabel_row=None)
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["download"] == 1
    assert s.calls["gate"] == 1  # detector 1회
    assert s.calls["insert"] == 1 and s.calls["complete"] == 1
    assert stats["ok"] == 1 and stats["failed"] == 0


# ── no bbox → level0 saved, level1 skipped, success ──

def test_no_bbox_level0_saved_job_succeeds():
    s = Spies(prelabel_row=None, temporal=_temporal(level0="ok", level1="no_bbox"))
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["insert"] == 1 and s.calls["complete"] == 1
    assert stats["ok"] == 1 and stats["failed"] == 0
    assert s.calls["fail"] == []


# ── R2 error → retryable ──

def test_r2_download_error_is_retryable():
    s = Spies(download_raises=True)
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["fail"] == [("job-clip-1", "r2_download_failed", True)]
    assert s.calls["compute"] == 0 and s.calls["insert"] == 0
    assert stats["failed"] == 1


# ── R2 원본 소실(404) → terminal source_media_missing (재시도해도 없음) ──

def test_r2_source_missing_is_terminal():
    s = Spies(download_exc=R2SourceMissing("r2 object missing (code=404)"))
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["fail"] == [("job-clip-1", "source_media_missing", False)]
    assert s.calls["compute"] == 0 and s.calls["insert"] == 0
    assert stats["terminal"] == 1


# ── R2 인증/권한 오류(403) → terminal r2_access_denied + cycle nonzero ──

def test_r2_access_denied_is_terminal():
    s = Spies(download_exc=R2AccessDenied("r2 access denied (code=403)"))
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["fail"] == [("job-clip-1", "r2_access_denied", False)]
    assert stats["terminal"] == 1 and stats["failed"] == 1  # failed>0 → cycle nonzero


# ── transient DB (insert) → retryable ──

def test_insert_db_error_is_retryable():
    s = Spies(prelabel_row={"id": "pre-1"}, insert_raises=True)
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["fail"] == [("job-clip-1", "db_transient", True)]
    assert stats["failed"] == 1


# ── invalid deterministic media → terminal allowlist code ──

def test_undecodable_media_is_terminal():
    s = Spies(prelabel_row=None, temporal=_temporal(level0="no_decodable_frames", level1="skipped"))
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["insert"] == 0  # decode 실패는 run 저장 안 함
    assert s.calls["fail"] == [("job-clip-1", "decode_no_frames", False)]  # retryable=False = terminal
    assert stats["terminal"] == 1


def test_compute_exception_is_terminal():
    s = Spies(prelabel_row={"id": "pre-1"}, compute_raises=True)
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["fail"] == [("job-clip-1", "temporal_compute_failed", False)]
    assert stats["terminal"] == 1


def test_detector_exception_is_retryable():
    s = Spies(prelabel_row=None, gate_raises=True)
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["fail"] == [("job-clip-1", "detector_failed", True)]
    assert stats["failed"] == 1


def test_missing_r2_key_is_terminal():
    s = Spies(prelabel_row=None)
    stats = _run_jobs([_job()], {}, s)  # clip_keys 에 r2_key 없음
    assert s.calls["download"] == 0
    assert s.calls["fail"][0][1] == "invalid_metadata"
    assert s.calls["fail"][0][2] is False  # terminal
    assert stats["terminal"] == 1


# ── per-clip isolation ──

def test_one_failure_isolates_others():
    s = Spies(prelabel_row={"id": "pre-1"}, temporal=_temporal(level1="ok"))
    jobs = [_job("clip-1"), _job("clip-2"), _job("clip-3")]
    keys = {"clip-1": "k1", "clip-3": "k3"}  # clip-2 는 r2_key 없음 → terminal

    stats = _run_jobs(jobs, keys, s)
    assert stats["reused"] == 2  # clip-1, clip-3 성공
    assert stats["failed"] == 1  # clip-2
    assert s.calls["complete"] == 2


# ── stale complete → skip, not failed ──

def test_stale_complete_is_not_failure():
    s = Spies(prelabel_row={"id": "pre-1"}, temporal=_temporal(level1="ok"),
              complete_raises=StaleJobError("stale"))
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert stats["failed"] == 0
    assert stats["stale"] == 1


# ── run(): feature flag / host guard / lock / empty claim ──

def test_run_disabled_exits_before_db(monkeypatch):
    monkeypatch.setattr(worker.config, "PYTHON_EVIDENCE_ENABLED", False)
    called = {"claim": 0}

    def claim(*a, **k):
        called["claim"] += 1
        return []

    rc = worker.run(sb=object(), now=NOW, hostname_fn=lambda: "mac-mini",
                    expected_host="mac-mini", claim_fn=claim,
                    acquire_lock_fn=lambda: object(), release_lock_fn=lambda fd: None)
    assert rc == 0
    assert called["claim"] == 0  # DB claim 미도달


def test_run_host_guard_fail_closed(monkeypatch):
    monkeypatch.setattr(worker.config, "PYTHON_EVIDENCE_ENABLED", True)
    lock_calls = {"acq": 0}
    rc = worker.run(sb=object(), now=NOW, hostname_fn=lambda: "attacker",
                    expected_host="mac-mini",
                    acquire_lock_fn=lambda: lock_calls.__setitem__("acq", lock_calls["acq"] + 1) or object(),
                    release_lock_fn=lambda fd: None, claim_fn=lambda *a, **k: [])
    assert rc == 2
    assert lock_calls["acq"] == 0  # lock 도 안 잡음


def test_run_lock_loser_is_noop(monkeypatch):
    monkeypatch.setattr(worker.config, "PYTHON_EVIDENCE_ENABLED", True)
    called = {"claim": 0}
    rc = worker.run(sb=object(), now=NOW, hostname_fn=lambda: "mac-mini", expected_host="mac-mini",
                    acquire_lock_fn=lambda: None,  # 다른 프로세스가 잡음
                    release_lock_fn=lambda fd: None,
                    claim_fn=lambda *a, **k: called.__setitem__("claim", called["claim"] + 1) or [])
    assert rc == 0
    assert called["claim"] == 0  # detector/R2/DB claim 0


def test_run_no_jobs_claims_but_no_processing(monkeypatch):
    monkeypatch.setattr(worker.config, "PYTHON_EVIDENCE_ENABLED", True)
    released = {"n": 0}
    calls = {"claim": 0, "load_keys": 0}

    def claim(sb, *, limit, worker_host, now):
        calls["claim"] += 1
        return []

    def load_keys(sb, ids):
        calls["load_keys"] += 1
        return {}

    rc = worker.run(sb=object(), now=NOW, hostname_fn=lambda: "mac-mini", expected_host="mac-mini",
                    acquire_lock_fn=lambda: object(),
                    release_lock_fn=lambda fd: released.__setitem__("n", released["n"] + 1),
                    claim_fn=claim, load_keys_fn=load_keys)
    assert rc == 0
    assert calls["claim"] == 1 and calls["load_keys"] == 0  # claim 만, R2/detector/temp 0
    assert released["n"] == 1  # lock 반드시 release


def test_run_releases_lock_on_exception(monkeypatch):
    monkeypatch.setattr(worker.config, "PYTHON_EVIDENCE_ENABLED", True)
    released = {"n": 0}

    def claim(*a, **k):
        raise RuntimeError("claim boom")

    with pytest.raises(RuntimeError):
        worker.run(sb=object(), now=NOW, hostname_fn=lambda: "mac-mini", expected_host="mac-mini",
                   acquire_lock_fn=lambda: object(),
                   release_lock_fn=lambda fd: released.__setitem__("n", released["n"] + 1),
                   claim_fn=claim)
    assert released["n"] == 1  # 예외에도 lock release


# ── 공통 Gate lock drift guard ──

def test_common_gate_lock_shares_activity_path():
    from reporter import activity_worker
    assert gate_lock.COMMON_GATE_LOCK_PATH == activity_worker._LOCK_PATH


# ── no forbidden writes (selector/VLM/behavior/app) — clip_prelabels 는 Gate evidence 라 허용 ──

def test_no_writes_to_forbidden_tables():
    from tests._fakes import FakeSB
    sb = FakeSB({})
    s = Spies(prelabel_row={"id": "pre-1"}, temporal=_temporal(level1="ok"))
    worker.process_jobs(
        sb=sb, jobs=[_job()], clip_keys={"clip-1": "k1"}, worker_host="mac-mini", producer=PRODUCER,
        gate_config=GATE_CFG, now=NOW, download_fn=s.download, find_prelabel_fn=s.find_prelabel,
        result_from_row_fn=s.result_from_row, run_gate_fn=s.run_gate, compute_fn=s.compute,
        insert_run_fn=s.insert_run, complete_fn=s.complete, fail_fn=s.fail,
    )
    # clip_prelabels 는 Gate evidence 쓰기(fresh gate) — 금지 아님. 판정/앱/GT 계열만 금지.
    for forbidden in ("clip_activity_assessments", "behavior_labels",
                      "behavior_logs", "clip_vlm_jobs", "camera_clips"):
        assert forbidden not in sb.store, f"worker wrote to forbidden table: {forbidden}"


# ── H2: fresh Gate → prelabel 저장 + run 에 사용 / min-frame terminal / threshold fail-closed ──

def test_fresh_gate_prelabel_used_in_run():
    # prelabel 없음 → run_gate 가 (result, 저장 row) 반환 → insert_run 에 그 row 가 prelabel 로 전달
    s = Spies(prelabel_row=None)
    _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["gate"] == 1
    assert s.insert_prelabels == [{"id": "fresh-pre-clip-1"}]  # fresh prelabel row 사용


def test_gate_insufficient_frames_terminal_no_store():
    s = Spies(prelabel_row=None, gate_insufficient=True)
    stats = _run_jobs([_job()], {"clip-1": "k1"}, s)
    assert s.calls["fail"] == [("job-clip-1", "decode_insufficient_frames", False)]
    assert s.calls["insert"] == 0  # prelabel 저장 안 함 + run 저장 안 함
    assert stats["terminal"] == 1


def test_make_sparse_gate_runner_persists_and_min_frames():
    loads = {"n": 0}
    stored = {"n": 0}

    def load_det(*a):
        loads["n"] += 1
        return object()

    def sample_ok(path, n):
        return [(0.0, None)] * 8  # 충분한 프레임

    def sample_few(path, n):
        return [(0.0, None)] * 2  # min 미달

    def prelabel(frames, **kw):
        return _prelabel_result()

    def motion(frames, result):
        return object()

    def store(sb, *, clip_id, result, motion, provenance, producer):
        stored["n"] += 1
        return {"id": f"pre-{clip_id}"}

    cfg = dict(GATE_CFG)
    rg = worker.make_sparse_gate_runner(cfg, min_frames=6, load_detector_fn=load_det,
                                        sample_fn=sample_ok, prelabel_fn=prelabel,
                                        motion_fn=motion, store_fn=store)
    result, row = rg(object(), "v.mp4", "clip-1", PRODUCER)
    assert row == {"id": "pre-clip-1"} and stored["n"] == 1 and loads["n"] == 1

    # 최소 프레임 미달 → detector 로드·저장 없이 InsufficientSampleFrames
    stored["n"] = 0
    loads["n"] = 0
    rg2 = worker.make_sparse_gate_runner(cfg, min_frames=6, load_detector_fn=load_det,
                                         sample_fn=sample_few, prelabel_fn=prelabel,
                                         motion_fn=motion, store_fn=store)
    with pytest.raises(InsufficientSampleFrames):
        rg2(object(), "v.mp4", "clip-1", PRODUCER)
    assert stored["n"] == 0 and loads["n"] == 0  # gecko absent 로 굳히지 않음


def test_run_invalid_threshold_fail_closed(monkeypatch):
    monkeypatch.setattr(worker.config, "PYTHON_EVIDENCE_ENABLED", True)
    monkeypatch.setattr(worker.config, "PYTHON_EVIDENCE_GATE_THRESHOLD", 0.0)  # 비정상
    called = {"lock": 0}
    rc = worker.run(sb=object(), now=NOW, hostname_fn=lambda: "mac-mini", expected_host="mac-mini",
                    acquire_lock_fn=lambda: called.__setitem__("lock", called["lock"] + 1) or object(),
                    release_lock_fn=lambda fd: None, claim_fn=lambda *a, **k: [])
    assert rc == 2
    assert called["lock"] == 0  # threshold fail-closed → lock 도 안 잡음
