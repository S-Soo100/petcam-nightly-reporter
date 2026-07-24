"""짧은 영상 retention service-role RPC adapter (설계 §4).

DB 정본(petcam-lab `926e5f6`)의 8개 RPC 를 얇게 감싼다. 원칙:
  - RPC 예외는 raw Supabase 원문(패스워드·URL·endpoint 섞일 수 있음)을 숨기고 `ShortClipStoreError`
    로 매핑(타입만 노출). worker 는 타입만 보고 판단한다.
  - false/stale complete·fail 은 성공이 아니다 → `StaleShortClipError`(설계 §4). worker 가 audit
    divergence 로 보고한다.
  - fail RPC 는 allowlist code 만 넘긴다(fingerprint 인자 없음 — DB 가 code 로부터 파생, §4 정정).
  - complete fingerprint 는 소문자 SHA-256 64-hex 만(RPC 도달 전 로컬 검증, DB CHECK 와 동일).
  - `motion_clip_system_exclusions` 는 bounded chunk 로만 읽는다(PostgREST 1000행 기본에 의존 금지).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from reporter.short_clip_retention_models import DeletionClaim, DetectionResult, ShortClipCandidate

_KST = ZoneInfo("Asia/Seoul")

# fail RPC allowlist(petcam-lab fn_fail_short_clip_media_delete). raw 예외 텍스트는 절대 안 넘긴다.
ALLOWED_FAIL_CODES = frozenset(
    {"r2_delete_failed", "audit_write_failed", "worker_host_mismatch", "internal_error"}
)
# quarantined/media_deleted 만 새 VLM 소비를 차단한다(candidate/restored/deletion_blocked 는 허용).
_VLM_BLOCKING_STATES = frozenset({"quarantined", "media_deleted"})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_EXCLUSION_CHUNK = 200  # bounded in-list; 1000행 PostgREST 기본에 의존하지 않는다.


class ShortClipStoreError(RuntimeError):
    """DB/RPC 계층 오류 위생 래퍼 — raw 원문(secret/URL/endpoint)을 담지 않는다."""


class StaleShortClipError(ShortClipStoreError):
    """complete/fail 이 자기 lease 가 아니어서 거부됨(false). 성공으로 보고하면 안 된다(§4)."""


def _safe_rpc(sb, name: str, args: dict):
    """RPC 호출 + 예외 위생. raw 에러를 숨기고 RPC 이름·예외 타입만 노출."""
    try:
        return sb.rpc(name, args).execute().data
    except ShortClipStoreError:
        raise
    except Exception as e:  # noqa: BLE001 — 위생: 타입만, raw 원문(secret/URL) 폐기
        raise ShortClipStoreError(f"rpc failed: {name} ({type(e).__name__})") from None


def _as_rows(data) -> list[dict]:
    """RPC 응답 정규화: None→[], 단일 object→[obj], list→그대로."""
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return [data]


def list_detection_candidates(sb, *, candidate_under_sec, cursor, limit) -> list[ShortClipCandidate]:
    """duration_sec < candidate_under_sec 후보를 (started_at,id) keyset 으로. cursor=(started_at,id)|None."""
    started_at, cursor_id = (cursor if cursor is not None else (None, None))
    data = _safe_rpc(sb, "fn_list_short_clip_detection_candidates", {
        "p_candidate_under_sec": candidate_under_sec,
        "p_cursor_started_at": started_at,
        "p_cursor_id": cursor_id,
        "p_limit": limit,
    })
    return [ShortClipCandidate.from_row(r) for r in _as_rows(data)]


def record_detection(sb, *, clip_id: str, now: datetime, write: bool) -> DetectionResult:
    """감지·격리 판정. caller 는 clip UUID·now·write 만 넘긴다(camera/정책/상태는 DB 재도출)."""
    data = _safe_rpc(sb, "fn_record_short_clip_detection", {
        "p_clip_id": clip_id, "p_now": now.isoformat(), "p_write": write,
    })
    rows = _as_rows(data)
    if not rows:
        raise ShortClipStoreError("record returned no route")
    return DetectionResult.from_row(rows[0])


def claim_media_deletions(sb, *, limit: int, worker_host: str, now: datetime) -> list[DeletionClaim]:
    """delete_after 만료 quarantined 를 최대 limit 개 claim(15분 lease). 반환은 exclusion/clip/key/token 만."""
    data = _safe_rpc(sb, "fn_claim_short_clip_media_deletions", {
        "p_limit": limit, "p_worker_host": worker_host, "p_now": now.isoformat(),
    })
    return [DeletionClaim.from_row(r) for r in _as_rows(data)]


def complete_media_delete(sb, *, exclusion_id: str, lease_token: str, fingerprint: str, now: datetime) -> None:
    """R2 삭제 성공 후 media_deleted 전환. fingerprint 는 소문자 SHA-256 64-hex. false=stale(성공 아님)."""
    if not _SHA256_HEX.match(fingerprint or ""):
        raise ValueError("fingerprint must be lowercase sha-256 hex(64)")
    ok = _safe_rpc(sb, "fn_complete_short_clip_media_delete", {
        "p_exclusion_id": exclusion_id, "p_lease_token": lease_token,
        "p_result_fingerprint": fingerprint, "p_now": now.isoformat(),
    })
    if not ok:
        raise StaleShortClipError("complete rejected (stale lease / not quarantined)")


def fail_media_delete(sb, *, exclusion_id: str, lease_token: str, code: str, now: datetime) -> None:
    """삭제 실패 기록. allowlist code 만(로컬 거부). fingerprint 인자 없음(DB 가 code 로부터 파생). false=stale."""
    if code not in ALLOWED_FAIL_CODES:
        raise ValueError(f"fail code not in allowlist: {code!r}")
    ok = _safe_rpc(sb, "fn_fail_short_clip_media_delete", {
        "p_exclusion_id": exclusion_id, "p_lease_token": lease_token,
        "p_result_code": code, "p_now": now.isoformat(),
    })
    if not ok:
        raise StaleShortClipError("fail rejected (stale lease / not quarantined)")


def claim_retention_notification(sb, *, summary_date_kst: date, worker_host: str, now: datetime) -> str | None:
    """KST 날짜 카드 claim. 활성 claim(미만료)/이미 전송이면 None(가로채기·중복 전송 차단)."""
    return _safe_rpc(sb, "fn_claim_short_clip_retention_notification", {
        "p_summary_date_kst": summary_date_kst.isoformat(), "p_worker_host": worker_host,
        "p_now": now.isoformat(),
    })


def complete_retention_notification(sb, *, summary_date_kst: date, claim_token: str, now: datetime) -> bool:
    """Slack 전송 성공 시 sent_at 기록. 자기 토큰이 아니면 False."""
    return bool(_safe_rpc(sb, "fn_complete_short_clip_retention_notification", {
        "p_summary_date_kst": summary_date_kst.isoformat(), "p_claim_token": claim_token,
        "p_now": now.isoformat(),
    }))


def release_retention_notification(sb, *, summary_date_kst: date, claim_token: str) -> bool:
    """Slack 실패 시 claim 해제(다음 사이클 재시도). 이미 전송된 것은 놓지 않는다(False)."""
    return bool(_safe_rpc(sb, "fn_release_short_clip_retention_notification", {
        "p_summary_date_kst": summary_date_kst.isoformat(), "p_claim_token": claim_token,
    }))


def _count_exclusions(sb, filters: list[tuple]) -> int:
    """motion_clip_system_exclusions 를 count='exact' 로 집계(전량 fetch 없이 정확한 count).

    PostgREST count=exact 는 data 페이지 상한과 무관하게 총 count 를 준다. raw 오류는 위생 처리.
    """
    try:
        q = sb.table("motion_clip_system_exclusions").select("clip_id", count="exact")
        for method, *args in filters:
            q = getattr(q, method)(*args)
        res = q.execute()
    except Exception as e:  # noqa: BLE001 — 위생
        raise ShortClipStoreError(f"aggregate query failed ({type(e).__name__})") from None
    return int(getattr(res, "count", None) or 0)


def aggregate_short_clip_daily(sb, *, now: datetime) -> dict[str, int]:
    """KST 날짜 경계로 DB 에서 직접 집계(설계 §8). per-cycle stats 가 아니라 원장 상태 기준.

    현재 상태 count(candidate/quarantined/deletion_blocked) + 그 날(KST) 이벤트(restored_at/
    media_deleted_at 이 [day_start,day_end)) 를 센다. review_pending·blocked 는 deletion_blocked
    현재 상태(검수 대기 = 삭제 보류), pending_delete 는 quarantined(7일 후 삭제 예정) 이다.
    """
    now_kst = now.astimezone(_KST)
    day_start = datetime(now_kst.year, now_kst.month, now_kst.day, tzinfo=_KST)
    day_end = day_start + timedelta(days=1)
    ds, de = day_start.isoformat(), day_end.isoformat()

    candidate = _count_exclusions(sb, [("eq", "state", "candidate")])
    quarantined = _count_exclusions(sb, [("eq", "state", "quarantined")])
    blocked = _count_exclusions(sb, [("eq", "state", "deletion_blocked")])
    restored = _count_exclusions(
        sb, [("eq", "state", "restored"), ("gte", "restored_at", ds), ("lt", "restored_at", de)]
    )
    deleted = _count_exclusions(
        sb,
        [("eq", "state", "media_deleted"), ("gte", "media_deleted_at", ds), ("lt", "media_deleted_at", de)],
    )
    return {
        "candidate": candidate,
        "quarantined": quarantined,
        "review_pending": blocked,
        "restored": restored,
        "pending_delete": quarantined,
        "deleted": deleted,
        "blocked": blocked,
    }


def load_system_excluded_clip_ids(sb, clip_ids) -> set[str]:
    """주어진 clip_id 중 quarantined|media_deleted 상태인 것만 bounded chunk 로 조회(설계 §6).

    새 VLM 소비 차단 전용 read. candidate/restored/deletion_blocked 는 차단하지 않는다.
    전량 select 없이 in-list chunk(200) 로만 조회 — PostgREST 1000행 기본에 의존하지 않는다.
    """
    ids = [c for c in dict.fromkeys(clip_ids) if c]  # 중복 제거 + None 제거(순서 보존)
    if not ids:
        return set()
    excluded: set[str] = set()
    for i in range(0, len(ids), _EXCLUSION_CHUNK):
        chunk = ids[i : i + _EXCLUSION_CHUNK]
        try:
            rows = (
                sb.table("motion_clip_system_exclusions")
                .select("clip_id, state")
                .in_("clip_id", chunk)
                .execute()
                .data
            )
        except Exception as e:  # noqa: BLE001 — 위생
            raise ShortClipStoreError(f"exclusion query failed ({type(e).__name__})") from None
        for r in rows or []:
            if r.get("state") in _VLM_BLOCKING_STATES and r.get("clip_id"):
                excluded.add(r["clip_id"])
    return excluded
