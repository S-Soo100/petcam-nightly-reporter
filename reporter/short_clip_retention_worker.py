"""짧은 영상 장치 오류 격리·보존 worker — metadata-only 감지 (설계 §3·§5).

Mac mini LaunchAgent(`com.petcam.short-clip-retention`, StartInterval=3600) 진입점.

    uv run python -m reporter.short_clip_retention_worker

흐름(plan Task 2):
  enabled check → host guard → flock → Supabase client → candidate keyset pagination → record RPC

경계:
  - **metadata-only**: R2 download, OpenCV, Gate, detector, local model, LLM/VLM 을 절대 호출하지 않는다.
    worker 는 clip UUID·timestamp·write flag 만 record RPC 에 넘기고, camera/duration/정책/보호/상태는
    DB 가 재도출한다. 집계 수만 출력(raw key/URL/UUID/token/fingerprint/예외 원문 금지).
  - 기본 feature flag false → DB client 생성 전 종료(migration 없는 환경 신규 table query 0).
  - host guard: expected-host fail-closed. lock/DB/R2/Slack 이전에 검사.
  - malformed detection route 는 한 clip 격리(count), DB-wide 오류는 cycle nonzero.
  - 삭제 cycle·Slack 은 Task 3 에서 이 run() 에 이어 붙는다(delete/Slack switch 는 그때 gate).
"""

from __future__ import annotations

import fcntl
import socket
import sys
from datetime import datetime, timezone

from supabase import create_client

from reporter import config
from reporter.short_clip_retention_store import (
    ShortClipStoreError,
    list_detection_candidates,
    record_detection,
)
from reporter.vlm_host_guard import HostOwnershipError, require_expected_host

_LOCK_PATH = "/tmp/petcam-short-clip-retention-worker.lock"
# 감지·집계 안정성: 무한 페이지 backstop(정상 종료는 page < batch_limit).
_MAX_PAGES = 100_000
_ROUTES = ("candidate", "quarantined", "protected", "reused", "reused_restored", "ineligible")


def acquire_retention_lock():
    """전용 nonblocking flock. 다른 인스턴스가 잡고 있으면 None(중복 실행 no-op)."""
    lock = open(_LOCK_PATH, "w")  # noqa: SIM115 — process lifetime flock
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except BlockingIOError:
        lock.close()
        return None


def release_retention_lock(lock) -> None:
    if lock is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def _empty_stats() -> dict[str, int]:
    stats = {route: 0 for route in _ROUTES}
    stats["failed"] = 0
    return stats


def run_detection(
    sb,
    *,
    now: datetime,
    write_enabled: bool,
    batch_limit: int,
    candidate_under_sec: float,
    list_candidates_fn=list_detection_candidates,
    record_fn=record_detection,
) -> dict[str, int]:
    """후보를 (started_at,id) keyset 으로 소진하며 record RPC 를 호출해 route 별로 집계한다.

    한 clip 의 malformed route(ValueError)는 격리(count)하고 계속. DB-wide(ShortClipStoreError)는
    상위로 전파해 cycle 을 nonzero 로 만든다.
    """
    stats = _empty_stats()
    cursor = None
    for _ in range(_MAX_PAGES):
        candidates = list_candidates_fn(
            sb, candidate_under_sec=candidate_under_sec, cursor=cursor, limit=batch_limit
        )
        if not candidates:
            break
        for cand in candidates:
            try:
                result = record_fn(sb, clip_id=cand.clip_id, now=now, write=write_enabled)
            except ValueError as exc:
                # malformed detection route = 한 clip 격리(다음 clip 계속). raw 없이 count.
                stats["failed"] += 1
                print(
                    f"[short-clip-retention] clip={cand.clip_id[:8]} record_malformed type={type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            stats[result.route] = stats.get(result.route, 0) + 1
        cursor = (candidates[-1].started_at, candidates[-1].clip_id)
        if len(candidates) < batch_limit:
            break
    return stats


def run(
    *,
    sb=None,
    now: datetime | None = None,
    enabled: bool | None = None,
    write_enabled: bool | None = None,
    expected_host: str | None = None,
    hostname: str | None = None,
    create_client_fn=create_client,
    list_candidates_fn=list_detection_candidates,
    record_fn=record_detection,
    acquire_lock_fn=acquire_retention_lock,
    release_lock_fn=release_retention_lock,
    batch_limit: int | None = None,
    candidate_under_sec: float | None = None,
) -> int:
    enabled = config.SHORT_CLIP_RETENTION_ENABLED if enabled is None else enabled
    if not enabled:
        print("[short-clip-retention] disabled — skip", flush=True)
        return 0

    write_enabled = (
        config.SHORT_CLIP_RETENTION_WRITE_ENABLED if write_enabled is None else write_enabled
    )
    expected_host = (
        config.SHORT_CLIP_RETENTION_EXPECTED_HOST if expected_host is None else expected_host
    )
    hostname = socket.gethostname() if hostname is None else hostname

    # host guard: lock/DB/R2/Slack 이전에 fail-closed(미설정/불일치 = nonzero).
    try:
        require_expected_host(hostname, expected_host)
    except HostOwnershipError as exc:
        print(
            f"[short-clip-retention] host_guard_failed type={type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    lock = acquire_lock_fn()
    if lock is None:
        print("[short-clip-retention] already running — skip", flush=True)
        return 0
    try:
        now = now or datetime.now(timezone.utc)
        sb = sb or create_client_fn(config.SUPABASE_URL, config.SUPABASE_KEY)
        batch_limit = batch_limit or config.SHORT_CLIP_RETENTION_BATCH_LIMIT
        candidate_under_sec = (
            candidate_under_sec
            if candidate_under_sec is not None
            else config.SHORT_CLIP_RETENTION_CANDIDATE_UNDER_SEC
        )
        stats = run_detection(
            sb,
            now=now,
            write_enabled=write_enabled,
            batch_limit=batch_limit,
            candidate_under_sec=candidate_under_sec,
            list_candidates_fn=list_candidates_fn,
            record_fn=record_fn,
        )
        print(
            "[short-clip-retention] "
            + " ".join(f"{key}={value}" for key, value in stats.items())
            + f" write_enabled={int(write_enabled)}",
            flush=True,
        )
        return 0
    except ShortClipStoreError as exc:
        # DB-wide 오류(list/record RPC 계층) — cycle nonzero, raw 없이 타입만.
        print(
            f"[short-clip-retention] batch_failed type={type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI 는 비밀 없는 타입만 출력하고 nonzero
        print(
            f"[short-clip-retention] batch_failed type={type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        release_lock_fn(lock)


if __name__ == "__main__":
    raise SystemExit(run())
