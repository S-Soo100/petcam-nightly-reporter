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

from gecko_vision_gate.activity_policy import ActivityPolicy, decide
from gecko_vision_gate.provenance import SAMPLER_VERSION, SCHEMA_VERSION, checkpoint_sha256

from reporter import config, r2
from reporter.activity_indexer import list_unprocessed_clips
from reporter.activity_settings import load_enabled_cameras
from reporter.activity_store import (
    ProducerInfo,
    find_prelabel,
    reconstruct_evidence,
    store_assessment_only,
    store_evidence_and_assessment,
)
from reporter.gate_runner import assess_clip, load_detector, model_version_for
from reporter.vlm_host_guard import HostOwnershipError, require_expected_host

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
    find_prelabel_fn=None,
    reconstruct_fn=None,
    store_assessment_fn=None,
    model_version: str = "",
    checkpoint_sha: str = "",
    num_frames: int = 12,
) -> dict:
    """clip 리스트를 순차 처리. 한 clip 실패는 batch 를 멈추지 않는다(격리). 반환 stats.

    하드닝 3: 같은 evidence identity(model/schema/checkpoint/threshold/sampler)의 prelabel 이
    이미 있으면 재추론(다운로드/inference) 없이 decide 만 재실행해 새 assessment 를 만든다
    (policy_version 만 바뀐 재평가). find_prelabel_fn 미주입이면 항상 신규 추론(기존 동작).
    """
    stats = {"clips": len(clips), "ok": 0, "reused": 0, "failed": 0,
             "decisions": dict(_EMPTY_DECISIONS), "durations": []}
    with tempfile.TemporaryDirectory() as tmp:
        for c in clips:
            dest = Path(tmp) / f"{c.id}.mp4"
            try:
                t0 = time.monotonic()
                existing = (
                    find_prelabel_fn(sb, c.id, model_version, SCHEMA_VERSION,
                                     checkpoint_sha, policy.gate_threshold, SAMPLER_VERSION, num_frames)
                    if find_prelabel_fn is not None else None
                )
                if existing is not None:
                    result, motion = reconstruct_fn(existing)
                    assessment = decide(result, motion, policy)
                    store_assessment_fn(sb, c.id, existing["id"], assessment, producer)
                    stats["reused"] += 1
                    stats["decisions"][assessment.decision] += 1
                else:
                    download_fn(c.r2_key, dest)
                    ga = assess_fn(str(dest), detector, policy, checkpoint_path, clip_id=c.id)
                    store_fn(sb, c, ga.result, ga.motion, ga.assessment, ga.provenance, producer)
                    stats["ok"] += 1
                    stats["decisions"][ga.assessment.decision] += 1
                stats["durations"].append(time.monotonic() - t0)
            except Exception as e:  # noqa: BLE001 — clip 격리, batch 계속 (DB/R2/decode 오류)
                stats["failed"] += 1
                # §5.3 로그 위생: 예외 전문({e})은 DB/HTTP 오류에 URL·secret 이 섞일 수 있어
                # 타입명만 남긴다. 실패량은 아래 summary 의 fail= 로 집계.
                print(f"[activity] clip {c.id[:8]} skip: {type(e).__name__}",
                      file=sys.stderr, flush=True)
            finally:
                dest.unlink(missing_ok=True)  # 임시 mp4 즉시 정리 (TemporaryDirectory 는 2차 방어)
    return stats


# versioned policy presets — 모든 임계값이 version 에 묶인다(재현성·감사). config 는 어느 version 을 쓸지만 고른다.
# activity-v0 = 최초 preflight 기준점(회귀 보존). activity-v1 = 0714 audit 반영(absent threshold↓ + static 민감도↑).
_POLICY_PRESETS: dict[str, dict] = {
    "activity-v0": {"gate_threshold": 0.25},
    "activity-v1": {"gate_threshold": 0.10, "roi_flow_active": 0.5},
}


def build_activity_policy(version: str | None = None) -> ActivityPolicy:
    """config 가 고른 policy version 의 preset 으로 ActivityPolicy 구성 (임계값 코드 상수 금지, §231)."""
    selected = version or config.ACTIVITY_POLICY_VERSION
    preset = _POLICY_PRESETS.get(selected)
    if preset is None:
        return ActivityPolicy(version=selected, gate_threshold=config.GATE_THRESHOLD)  # 미등록 = config 폴백
    return ActivityPolicy(version=selected, **preset)


def acquire_activity_lock():
    fd = open(_LOCK_PATH, "w")  # noqa: SIM115 — lock 은 프로세스 수명 동안 열려 있어야
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


def release_activity_lock(fd) -> None:
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
        f"ok={stats['ok']} reused={stats.get('reused', 0)} fail={stats['failed']} "
        f"active={d['active']} absent={d['exclude_absent']} static={d['exclude_static']} unknown={d['unknown']} "
        f"avg={avg:.2f}s max={mx:.2f}s model={model_version} policy={policy.version}",
        flush=True,
    )


def _filter_by_policy(settings, worker_version: str):
    """active_policy_version == worker_version 인 카메라만 통과. null/불일치는 그 카메라만 skip + 로그.

    A단계 guard(§14): 잘못된 policy 로 evidence/assessment 를 저장하지 않기 위한 fail-open. 전체 batch 는
    중단하지 않고 일치 카메라만 처리한다. 불일치 카메라는 여기서 걸러져 clip 조회/다운로드/추론에 도달하지 않는다.
    """
    matched = []
    for s in settings:
        if s.active_policy_version == worker_version:
            matched.append(s)
        else:
            got = s.active_policy_version if s.active_policy_version is not None else "null"
            print(f"[activity] camera {s.camera_id[:8]} policy mismatch settings={got} worker={worker_version} — skip",
                  flush=True)
    return matched


def run(
    *,
    sb=None,
    now: datetime | None = None,
    load_detector_fn=load_detector,
    download_fn=None,
    assess_fn=None,
    acquire_lock_fn=acquire_activity_lock,
    release_lock_fn=release_activity_lock,
    hostname_fn=socket.gethostname,
    expected_host=None,
) -> int:
    now = now or datetime.now(timezone.utc)
    # §5.1 fail-closed host guard: lock·create_client·DB·R2·detector·Slack 이전에 먼저 판정.
    # installer 자동 승인 방지 위해 expected 는 배포자가 명시한 config 값을 쓴다(short↔FQDN 자동 동치 금지).
    expected = config.ACTIVITY_EXPECTED_HOST if expected_host is None else expected_host
    try:
        require_expected_host(hostname_fn(), expected)
    except HostOwnershipError as e:
        print(f"[activity] host guard fail-closed: {e}", flush=True)
        return 2
    download_fn = download_fn if download_fn is not None else r2.download_clip
    assess_fn = assess_fn if assess_fn is not None else assess_clip
    lock_fd = acquire_lock_fn()
    if lock_fd is None:
        print("[activity] already running (flock) — skip", flush=True)
        return 0
    try:
        sb = sb or create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        settings = load_enabled_cameras(sb)
        if not settings:
            print(f"[activity] {now:%m-%d %H:%M} no enabled cameras — skip", flush=True)
            return 0
        # A단계 guard: worker policy 와 카메라 active_policy_version 이 일치하는 카메라만 처리(null/불일치 skip).
        worker_version = config.ACTIVITY_POLICY_VERSION
        matched = _filter_by_policy(settings, worker_version)
        if not matched:
            print(f"[activity] {now:%m-%d %H:%M} no matching policy cameras (worker={worker_version}) — skip",
                  flush=True)
            return 0
        camera_ids = [s.camera_id for s in matched]
        policy = build_activity_policy()
        checkpoint = config.GATE_CHECKPOINT_PATH
        model_version = model_version_for(checkpoint)
        ckpt_sha = checkpoint_sha256(checkpoint)
        start = now - timedelta(hours=config.ACTIVITY_WINDOW_HOURS)
        # min_frames 를 policy 에서 명시 전달 — indexer 의 완료 판정(완전 prelabel 링크)과
        # assess_clip 의 최소 프레임 barrier 가 같은 정책 최소치를 공유하게 한다(설계 §5).
        clips = list_unprocessed_clips(
            sb, camera_ids, policy.version, start, now,
            min_frames=policy.min_frames, limit=config.ACTIVITY_BATCH_LIMIT,
        )
        if not clips:
            print(f"[activity] {now:%m-%d %H:%M} cameras={len(camera_ids)} no unprocessed clips", flush=True)
            return 0
        detector = load_detector_fn(checkpoint, policy.gate_threshold)
        producer = ProducerInfo(host=socket.gethostname(), run_id=f"{now:%Y%m%dT%H%M%S}")
        stats = process_batch(
            sb, clips, detector, policy, checkpoint, producer,
            download_fn=download_fn, assess_fn=assess_fn, store_fn=store_evidence_and_assessment,
            find_prelabel_fn=find_prelabel, reconstruct_fn=reconstruct_evidence,
            store_assessment_fn=store_assessment_only,
            model_version=model_version, checkpoint_sha=ckpt_sha,
        )
        _log(now, len(camera_ids), stats, policy, model_version)
        # §5.3: 성공 clip 결과는 유지하되, 하나라도 실패하면 cycle 을 정상(exit 0)으로 숨기지 않는다.
        # 모든 clip 이 ok/reused 일 때만 0. early-return(카메라·clip 없음)은 실패 아님 → 위에서 0.
        return 1 if stats["failed"] else 0
    finally:
        release_lock_fn(lock_fd)


if __name__ == "__main__":
    sys.exit(run())
