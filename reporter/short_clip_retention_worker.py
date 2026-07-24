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
from zoneinfo import ZoneInfo

from supabase import create_client

from reporter import config, r2
from reporter.short_clip_retention_store import (
    ShortClipStoreError,
    claim_media_deletions,
    claim_retention_notification,
    complete_media_delete,
    complete_retention_notification,
    fail_media_delete,
    list_detection_candidates,
    record_detection,
    release_retention_notification,
)
from reporter.short_clip_retention_summary import format_short_clip_retention_summary
from reporter.slack import post_slack
from reporter.vlm_host_guard import HostOwnershipError, require_expected_host

_KST = ZoneInfo("Asia/Seoul")
_DEVICE_LABEL = "Mac mini"
_RULE_VERSION = "short-device-error-v1"

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


def run_delete_cycle(
    sb,
    *,
    now: datetime,
    worker_host: str,
    limit: int,
    claim_fn=claim_media_deletions,
    delete_object_fn=r2.delete_clip_object,
    complete_fn=complete_media_delete,
    fail_fn=fail_media_delete,
) -> dict[str, int]:
    """DB claim(최대 limit) → exact object 삭제 → complete/fail (설계 §7).

    각 claim: 메모리에서 sha256(r2_key) 계산 → 정확히 그 object 삭제 → complete(fingerprint).
    R2 실패 → fail(r2_delete_failed) 후 계속(다른 object 굶기지 않음). R2 성공인데 complete false/
    error = audit divergence → 성공으로 계속 주장하지 않고 cycle 중단(worker 가 nonzero 로 보고).
    raw key/endpoint/exception 은 로그에 담지 않는다.
    """
    stats = {"claimed": 0, "deleted": 0, "delete_failed": 0, "audit_divergence": 0}
    claims = claim_fn(sb, limit=limit, worker_host=worker_host, now=now)
    stats["claimed"] = len(claims)
    for claim in claims:
        fingerprint = claim.key_fingerprint()  # in-memory sha256(r2_key) 소문자 64-hex
        try:
            delete_object_fn(claim.r2_key)
        except Exception as exc:  # noqa: BLE001 — R2 실패는 allowlist code 로 fail + 계속
            try:
                fail_fn(
                    sb, exclusion_id=claim.exclusion_id, lease_token=claim.lease_token,
                    code="r2_delete_failed", now=now,
                )
            except ShortClipStoreError:
                pass  # fail 도 stale/오류면 다른 worker 가 lease 회수 — raw 없이 무시
            stats["delete_failed"] += 1
            print(
                f"[short-clip-retention] delete_failed type={type(exc).__name__}",
                file=sys.stderr, flush=True,
            )
            continue
        try:
            complete_fn(
                sb, exclusion_id=claim.exclusion_id, lease_token=claim.lease_token,
                fingerprint=fingerprint, now=now,
            )
        except ShortClipStoreError as exc:
            # R2 삭제됐는데 DB 미기록(false/stale/error) — 성공 계속 주장 금지, cycle 중단.
            stats["audit_divergence"] += 1
            print(
                f"[short-clip-retention] audit_divergence type={type(exc).__name__}",
                file=sys.stderr, flush=True,
            )
            break
        stats["deleted"] += 1
    return stats


def maybe_send_slack(
    sb,
    *,
    now: datetime,
    worker_host: str,
    stats: dict,
    report_hour: int,
    claim_fn=claim_retention_notification,
    complete_fn=complete_retention_notification,
    release_fn=release_retention_notification,
    post_fn=post_slack,
    summary_fn=format_short_clip_retention_summary,
) -> None:
    """KST 리포트 시각 이후 내구성 1일 카드 1회. claim → post → 성공 complete / 실패 release.

    리포트 시각 이전 no-op cycle 은 Slack 0. 활성 claim(다른 worker)/이미 전송 이면 claim=None → 0.
    카드는 count/안전 라벨만(raw key/URL/UUID/token/fingerprint 없음, summary_fn 이 보장).
    """
    now_kst = now.astimezone(_KST)
    if now_kst.hour < report_hour:
        return
    day = now_kst.date()
    token = claim_fn(sb, summary_date_kst=day, worker_host=worker_host, now=now)
    if not token:
        return  # 이미 전송됨 / 활성 claim → 중복 전송 없음
    text = summary_fn(stats, now_kst)
    if post_fn(text):
        complete_fn(sb, summary_date_kst=day, claim_token=token, now=now)
    else:
        release_fn(sb, summary_date_kst=day, claim_token=token)


def run(
    *,
    sb=None,
    now: datetime | None = None,
    enabled: bool | None = None,
    write_enabled: bool | None = None,
    expected_host: str | None = None,
    hostname: str | None = None,
    delete_enabled: bool | None = None,
    delete_limit: int | None = None,
    report_hour: int | None = None,
    create_client_fn=create_client,
    list_candidates_fn=list_detection_candidates,
    record_fn=record_detection,
    acquire_lock_fn=acquire_retention_lock,
    release_lock_fn=release_retention_lock,
    claim_deletions_fn=claim_media_deletions,
    delete_object_fn=r2.delete_clip_object,
    complete_delete_fn=complete_media_delete,
    fail_delete_fn=fail_media_delete,
    post_slack_fn=post_slack,
    claim_notification_fn=claim_retention_notification,
    complete_notification_fn=complete_retention_notification,
    release_notification_fn=release_retention_notification,
    summary_fn=format_short_clip_retention_summary,
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
    delete_enabled = (
        config.SHORT_CLIP_RETENTION_DELETE_ENABLED if delete_enabled is None else delete_enabled
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
        report_hour = (
            config.SHORT_CLIP_RETENTION_REPORT_HOUR_KST if report_hour is None else report_hour
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

        # 삭제 cycle: delete switch 가 켜졌을 때만. 꺼져 있으면 claim/R2 0.
        delete_stats = {"claimed": 0, "deleted": 0, "delete_failed": 0, "audit_divergence": 0}
        if delete_enabled:
            delete_stats = run_delete_cycle(
                sb,
                now=now,
                worker_host=hostname,
                limit=delete_limit or config.SHORT_CLIP_RETENTION_DELETE_LIMIT,
                claim_fn=claim_deletions_fn,
                delete_object_fn=delete_object_fn,
                complete_fn=complete_delete_fn,
                fail_fn=fail_delete_fn,
            )
            print(
                "[short-clip-retention] delete "
                + " ".join(f"{key}={value}" for key, value in delete_stats.items())
                + f" delete_enabled={int(delete_enabled)}",
                flush=True,
            )

        # 내구성 일일 Slack 카드(best-effort). 실패해도 감지/삭제 결과를 뒤집지 않는다.
        slack_stats = {
            "candidate": stats["candidate"],
            "quarantined": stats["quarantined"],
            "review_pending": stats["protected"],
            "restored": 0,
            "pending_delete": stats["quarantined"],
            "deleted": delete_stats["deleted"],
            "blocked": delete_stats["delete_failed"],
            "device_label": _DEVICE_LABEL,
            "rule_version": _RULE_VERSION,
        }
        try:
            maybe_send_slack(
                sb,
                now=now,
                worker_host=hostname,
                stats=slack_stats,
                report_hour=report_hour,
                claim_fn=claim_notification_fn,
                complete_fn=complete_notification_fn,
                release_fn=release_notification_fn,
                post_fn=post_slack_fn,
                summary_fn=summary_fn,
            )
        except Exception as exc:  # noqa: BLE001 — Slack/notification 은 best-effort, raw 없이 타입만
            print(
                f"[short-clip-retention] slack_skipped type={type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )

        # audit divergence(R2 삭제됐는데 DB 미기록)만 cycle 을 nonzero 로 만든다.
        return 1 if delete_stats["audit_divergence"] > 0 else 0
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
