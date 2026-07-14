"""활동필터 worker — motion_clips 미처리 → Gate evidence + four-state 판정 저장.

기존 reporter.worker(Claude 상황판)와 **완전 독립**한 별도 entrypoint. Claude/VLM 호출 0회.
detector 1회 로드 후 batch 재사용, clip 단위 오류 격리, 멱등 upsert, flock 중복방지,
TemporaryDirectory 정리. 설정(allowlist) 없으면 0건 — 새 카메라 자동 적용 안 됨(지시문 §95).

    uv run python -m reporter.activity_worker
"""

from __future__ import annotations

import fcntl
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from supabase import create_client

from gecko_vision_gate.activity_policy import ActivityPolicy
from gecko_vision_gate.provenance import SCHEMA_VERSION

from reporter import config, r2
from reporter.activity_indexer import list_unprocessed_clips
from reporter.activity_settings import load_enabled_cameras
from reporter.activity_store import ProducerInfo, store_evidence_and_assessment
from reporter.gate_runner import assess_clip, load_detector, model_version_for

_LOCK_PATH = "/tmp/petcam-activity-worker.lock"
_EMPTY_DECISIONS = {"active": 0, "exclude_absent": 0, "exclude_static": 0, "unknown": 0}


def process_batch(
    sb,
    clips,
    detector,
    policy: ActivityPolicy,
    checkpoint_path: str,
    producer: ProducerInfo,
    *,
    download_fn,
    assess_fn,
    store_fn,
) -> dict:
    """clip 리스트를 순차 처리. 한 clip 실패는 batch 를 멈추지 않는다(격리). 반환 stats."""
    stats = {"clips": len(clips), "ok": 0, "failed": 0,
             "decisions": dict(_EMPTY_DECISIONS), "durations": []}
    with tempfile.TemporaryDirectory() as tmp:
        for c in clips:
            dest = Path(tmp) / f"{c.id}.mp4"
            try:
                t0 = time.monotonic()
                download_fn(c.r2_key, dest)
                ga = assess_fn(str(dest), detector, policy, checkpoint_path, clip_id=c.id)
                store_fn(sb, c, ga.result, ga.motion, ga.assessment, ga.provenance, producer)
                stats["ok"] += 1
                stats["decisions"][ga.assessment.decision] += 1
                stats["durations"].append(time.monotonic() - t0)
            except Exception as e:  # noqa: BLE001 — clip 격리, batch 계속 (DB/R2/decode 오류)
                stats["failed"] += 1
                print(f"[activity] clip {c.id[:8]} skip: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
            finally:
                dest.unlink(missing_ok=True)  # 임시 mp4 즉시 정리 (TemporaryDirectory 는 2차 방어)
    return stats


def _build_policy() -> ActivityPolicy:
    """versioned policy 를 config 에서 주입 (임계값 코드 상수 금지, 지시문 §231)."""
    return ActivityPolicy(version=config.ACTIVITY_POLICY_VERSION, gate_threshold=config.GATE_THRESHOLD)


def _acquire_lock():
    fd = open(_LOCK_PATH, "w")  # noqa: SIM115 — lock 은 프로세스 수명 동안 열려 있어야
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


def _release_lock(fd) -> None:
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()


def _log(now: datetime, cameras: int, stats: dict, policy: ActivityPolicy, model_version: str) -> None:
    durs = stats["durations"]
    avg = sum(durs) / len(durs) if durs else 0.0
    mx = max(durs) if durs else 0.0
    d = stats["decisions"]
    print(
        f"[activity] {now:%m-%d %H:%M} cameras={cameras} queried={stats['clips']} "
        f"ok={stats['ok']} fail={stats['failed']} "
        f"active={d['active']} absent={d['exclude_absent']} static={d['exclude_static']} unknown={d['unknown']} "
        f"avg={avg:.2f}s max={mx:.2f}s model={model_version} policy={policy.version}",
        flush=True,
    )


def run(*, sb=None, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    lock_fd = _acquire_lock()
    if lock_fd is None:
        print("[activity] already running (flock) — skip", flush=True)
        return 0
    try:
        sb = sb or create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        settings = load_enabled_cameras(sb)
        if not settings:
            print(f"[activity] {now:%m-%d %H:%M} no enabled cameras — skip", flush=True)
            return 0
        camera_ids = [s.camera_id for s in settings]
        policy = _build_policy()
        checkpoint = config.GATE_CHECKPOINT_PATH
        model_version = model_version_for(checkpoint)
        start = now - timedelta(hours=config.ACTIVITY_WINDOW_HOURS)
        clips = list_unprocessed_clips(
            sb, camera_ids, model_version, SCHEMA_VERSION, start, now, limit=config.ACTIVITY_BATCH_LIMIT
        )
        if not clips:
            print(f"[activity] {now:%m-%d %H:%M} cameras={len(camera_ids)} no unprocessed clips", flush=True)
            return 0
        detector = load_detector(checkpoint, policy.gate_threshold)
        producer = ProducerInfo(host=socket.gethostname(), run_id=f"{now:%Y%m%dT%H%M%S}")
        stats = process_batch(
            sb, clips, detector, policy, checkpoint, producer,
            download_fn=r2.download_clip, assess_fn=assess_clip, store_fn=store_evidence_and_assessment,
        )
        _log(now, len(camera_ids), stats, policy, model_version)
        return 0
    finally:
        _release_lock(lock_fd)


if __name__ == "__main__":
    sys.exit(run())
