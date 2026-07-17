"""전 영상 Python evidence worker — durable queue 를 소비해 Level 0/1 raw evidence 를 append-only 저장.

activity_worker 와 **완전 독립**한 별도 entrypoint. Claude/VLM/selector/behavior/app 호출 0회.
"모든 motion_clips 에 evidence 가 존재한다"만 자기 책임으로 갖고, 계산·저장은 Gate/store 기존 모듈을
재사용한다(설계 §4).

    uv run python -m reporter.python_evidence_worker

흐름(plan Task 5):
  require_expected_host → 공통 Gate lock → claim → (job 없으면 R2/detector/temp 0 로 종료)
  → job 별: download 1회 → 기존 prelabel 있으면 재사용(detector 0) / 없으면 sparse Gate 1회
  → compute_temporal_evidence → insert_run(멱등) → complete_job

경계:
  - 기본 feature flag false → DB client 생성 전 종료(migration 없는 환경 신규 table query 0, §203).
  - runtime host: expected-host fail-closed. 공통 Gate lock 을 DB/R2/model load 전에 획득.
  - clip 별 오류 격리(하나 실패해도 batch 계속), cycle 에 실패 있으면 nonzero.
  - selector/VLM/behavior/GT/activity/app 은 절대 호출·변경하지 않는다.
"""

from __future__ import annotations

import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from supabase import create_client

from gecko_vision_gate.frame_sampling import sample_frames
from gecko_vision_gate.prelabel import prelabel_from_frames
from gecko_vision_gate.provenance import SAMPLER_VERSION, SCHEMA_VERSION, checkpoint_sha256
from gecko_vision_gate.temporal_evidence import ALGORITHM_VERSION, compute_temporal_evidence

from reporter import config, r2
from reporter.activity_store import find_prelabel, reconstruct_evidence
from reporter.gate_lock import acquire_common_gate_lock, release_common_gate_lock
from reporter.gate_runner import load_detector, model_version_for
from reporter.python_evidence_store import (
    EvidenceStoreError,
    ProducerInfo,
    StaleJobError,
    claim_jobs,
    complete_job,
    fail_job,
    insert_run,
    load_clip_r2_keys,
)
from reporter.vlm_host_guard import HostOwnershipError, require_expected_host

# level0 decode 실패 → job terminal 코드 매핑 (allowlist). decode 불가 media 는 재시도해도 같으므로 terminal.
_DECODE_TERMINAL = {
    "no_decodable_frames": "decode_no_frames",
    "insufficient_decodable_frames": "decode_insufficient_frames",
    "invalid_metadata": "invalid_metadata",
}


class _JobFailure(Exception):
    """job 단위 실패를 실패코드+retry 여부와 함께 나른다(내부 제어 흐름용)."""

    def __init__(self, code: str, *, retryable: bool):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def make_sparse_gate_runner(gate_config: dict, load_detector_fn=load_detector):
    """sparse Gate prelabel(detector) 러너. detector 는 첫 호출에서 1회만 로드(배치 재사용)."""
    state: dict = {"detector": None}

    def run_gate(video_path: str, clip_id: str):
        if state["detector"] is None:
            state["detector"] = load_detector_fn(
                gate_config["checkpoint_path"], gate_config["threshold"], gate_config["model_size"]
            )
        frames = sample_frames(video_path, gate_config["num_frames"])
        return prelabel_from_frames(
            frames,
            threshold=gate_config["threshold"],
            model_size=gate_config["model_size"],
            checkpoint=gate_config["checkpoint_path"],
            clip_id=clip_id,
            detector=state["detector"],
        )

    return run_gate


def _process_one(sb, job, dest, clip_keys, *, gate_config, producer, worker_host,
                 download_fn, find_prelabel_fn, reconstruct_fn, run_gate_fn, compute_fn,
                 insert_run_fn, complete_fn) -> str:
    """job 1건 처리. 성공 시 'ok'|'reused'|'stale' 반환, 실패는 _JobFailure raise."""
    r2_key = clip_keys.get(job.clip_id)
    if not r2_key:
        raise _JobFailure("invalid_metadata", retryable=False)  # 다운로드 소스 없음 = 영구

    try:
        download_fn(r2_key, dest)
    except Exception as e:  # noqa: BLE001 — R2 일시 오류는 정상(재시도 대상)
        raise _JobFailure("r2_download_failed", retryable=True) from e

    # 기존 prelabel 재사용(detector 재호출 금지). clip_prelabels 는 read-only 로만 본다.
    try:
        prelabel_row = find_prelabel_fn(
            sb, job.clip_id, gate_config["model_version"], gate_config["schema_version"],
            gate_config["checkpoint_sha256"], gate_config["threshold"],
            gate_config["sampler_version"], gate_config["num_frames"],
        )
    except Exception as e:  # noqa: BLE001 — DB 조회 일시 오류
        raise _JobFailure("db_transient", retryable=True) from e

    reused = prelabel_row is not None
    if reused:
        result, _motion = reconstruct_fn(prelabel_row)
    else:
        try:
            result = run_gate_fn(str(dest), job.clip_id)  # detector 1회(lazy load)
        except Exception as e:  # noqa: BLE001 — detector/모델 일시 오류는 재시도
            raise _JobFailure("detector_failed", retryable=True) from e

    try:
        temporal = compute_fn(str(dest), result)
    except Exception as e:  # noqa: BLE001 — deterministic media 실패 = terminal
        raise _JobFailure("temporal_compute_failed", retryable=False) from e

    if temporal.level0_status != "ok":
        # decode 불가/미달 = terminal. terminal reason(failure_code)이 곧 decode evidence(설계 §7.1).
        code = _DECODE_TERMINAL.get(temporal.level0_status, "decode_no_frames")
        raise _JobFailure(code, retryable=False)

    try:
        run = insert_run_fn(sb, job=job, temporal=temporal, prelabel=prelabel_row, producer=producer)
    except EvidenceStoreError as e:
        raise _JobFailure("db_transient", retryable=True) from e

    try:
        complete_fn(sb, job_id=job.id, run_id=run["id"], worker_host=worker_host)
    except StaleJobError:
        # 다른 worker 가 lease 를 회수해 이미 처리 중/완료. run 은 멱등 저장됐으므로 no-op.
        return "stale"
    except EvidenceStoreError as e:
        raise _JobFailure("db_transient", retryable=True) from e
    return "reused" if reused else "ok"


def process_jobs(sb, jobs, clip_keys, *, worker_host, producer, gate_config, now,
                 download_fn, find_prelabel_fn, reconstruct_fn, run_gate_fn, compute_fn,
                 insert_run_fn, complete_fn, fail_fn) -> dict:
    """claim 된 job 들을 순차 처리. clip 별 오류 격리, temp media 0(TemporaryDirectory + 즉시 unlink)."""
    stats = {"jobs": len(jobs), "ok": 0, "reused": 0, "stale": 0, "failed": 0, "terminal": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for job in jobs:
            dest = Path(tmp) / f"{job.clip_id}.mp4"
            try:
                outcome = _process_one(
                    sb, job, dest, clip_keys, gate_config=gate_config, producer=producer,
                    worker_host=worker_host, download_fn=download_fn, find_prelabel_fn=find_prelabel_fn,
                    reconstruct_fn=reconstruct_fn, run_gate_fn=run_gate_fn, compute_fn=compute_fn,
                    insert_run_fn=insert_run_fn, complete_fn=complete_fn,
                )
                stats[outcome] += 1
            except _JobFailure as jf:
                _safe_fail(sb, job, jf.code, jf.retryable, worker_host, now, fail_fn)
                stats["failed"] += 1
                if not jf.retryable:
                    stats["terminal"] += 1
            except Exception as e:  # noqa: BLE001 — 예상 못한 오류도 clip 격리(다음 job 계속)
                _safe_fail(sb, job, "internal_error", True, worker_host, now, fail_fn)
                stats["failed"] += 1
                print(f"[pyevidence] job {job.id[:8]} unexpected: {type(e).__name__}",
                      file=sys.stderr, flush=True)
            finally:
                dest.unlink(missing_ok=True)  # 임시 mp4 즉시 정리(temp media 0)
    return stats


def _safe_fail(sb, job, code, retryable, worker_host, now, fail_fn) -> None:
    """실패 기록은 best-effort. DB 가 죽어 fail 도 못 남기면 lease 만료 회수가 대신 처리한다."""
    try:
        fail_fn(sb, job_id=job.id, failure_code=code, retryable=retryable,
                worker_host=worker_host, now=now)
    except Exception as e:  # noqa: BLE001 — 로그 위생: 타입만
        print(f"[pyevidence] fail-record failed job={job.id[:8]}: {type(e).__name__}",
              file=sys.stderr, flush=True)


def _log(now: datetime, stats: dict, host: str) -> None:
    print(
        f"[pyevidence] {now:%m-%d %H:%M} host={host} jobs={stats['jobs']} "
        f"ok={stats['ok']} reused={stats['reused']} stale={stats['stale']} "
        f"fail={stats['failed']} terminal={stats['terminal']}",
        flush=True,
    )


def run(
    *,
    sb=None,
    now: datetime | None = None,
    hostname_fn=socket.gethostname,
    expected_host: str | None = None,
    acquire_lock_fn=acquire_common_gate_lock,
    release_lock_fn=release_common_gate_lock,
    claim_fn=claim_jobs,
    load_keys_fn=load_clip_r2_keys,
    make_gate_fn=make_sparse_gate_runner,
    download_fn=None,
    compute_fn=compute_temporal_evidence,
    find_prelabel_fn=find_prelabel,
    reconstruct_fn=reconstruct_evidence,
    insert_run_fn=insert_run,
    complete_fn=complete_job,
    fail_fn=fail_job,
    checkpoint_sha_fn=checkpoint_sha256,
    model_version_fn=model_version_for,
    load_detector_fn=load_detector,
) -> int:
    now = now or datetime.now(timezone.utc)
    # 1) feature flag: 비활성이면 DB client 생성 전에 즉시 종료(신규 table query 0).
    if not config.PYTHON_EVIDENCE_ENABLED:
        print("[pyevidence] disabled (PYTHON_EVIDENCE_ENABLED=0) — skip", flush=True)
        return 0
    # 2) fail-closed host guard (lock/DB/R2/detector 이전). installer 자동 승인 금지 → config 명시값.
    host = hostname_fn()
    expected = config.PYTHON_EVIDENCE_EXPECTED_HOST if expected_host is None else expected_host
    try:
        require_expected_host(host, expected)
    except HostOwnershipError as e:
        print(f"[pyevidence] host guard fail-closed: {e}", flush=True)
        return 2
    # 3) 공통 Gate lock (detector 동시 실행 방지). loser 는 clean no-op.
    lock_fd = acquire_lock_fn()
    if lock_fd is None:
        print("[pyevidence] common gate lock busy — skip", flush=True)
        return 0
    try:
        sb = sb or create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        jobs = claim_fn(sb, limit=config.PYTHON_EVIDENCE_BATCH_LIMIT, worker_host=host, now=now)
        if not jobs:
            print(f"[pyevidence] {now:%m-%d %H:%M} no jobs — skip", flush=True)
            return 0  # detector/R2/temp 0
        clip_keys = load_keys_fn(sb, [j.clip_id for j in jobs])
        checkpoint = config.GATE_CHECKPOINT_PATH
        gate_config = {
            "checkpoint_path": checkpoint,
            "threshold": config.GATE_THRESHOLD,
            "num_frames": 12,
            "model_size": "nano",
            "model_version": model_version_fn(checkpoint),
            "checkpoint_sha256": checkpoint_sha_fn(checkpoint),
            "sampler_version": SAMPLER_VERSION,
            "schema_version": SCHEMA_VERSION,
        }
        download_fn = download_fn if download_fn is not None else r2.download_clip
        run_gate_fn = make_gate_fn(gate_config, load_detector_fn)
        producer = ProducerInfo(
            host=host, run_id=f"{now:%Y%m%dT%H%M%S}", code_ref=f"python-evidence-worker/{ALGORITHM_VERSION}"
        )
        stats = process_jobs(
            sb, jobs, clip_keys, worker_host=host, producer=producer, gate_config=gate_config,
            now=now, download_fn=download_fn, find_prelabel_fn=find_prelabel_fn,
            reconstruct_fn=reconstruct_fn, run_gate_fn=run_gate_fn, compute_fn=compute_fn,
            insert_run_fn=insert_run_fn, complete_fn=complete_fn, fail_fn=fail_fn,
        )
        _log(now, stats, host)
        # 하나라도 실패면 cycle 을 정상(0)으로 숨기지 않는다. 성공 clip 결과는 보존.
        return 1 if stats["failed"] else 0
    finally:
        release_lock_fn(lock_fd)


if __name__ == "__main__":
    sys.exit(run())
