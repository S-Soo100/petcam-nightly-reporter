"""짧은 영상 retention Supabase RPC adapter 계약 테스트 (설계 §4). 실 DB 무의존(recording fake)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from reporter.short_clip_retention_store import (
    ShortClipStoreError,
    StaleShortClipError,
    aggregate_short_clip_daily,
    claim_media_deletions,
    claim_retention_notification,
    complete_media_delete,
    complete_retention_notification,
    fail_media_delete,
    list_detection_candidates,
    record_detection,
    release_retention_notification,
)

NOW = datetime(2026, 7, 25, 0, 0, 0, tzinfo=timezone.utc)  # = 2026-07-25 09:00 KST


class _CountResp:
    def __init__(self, data):
        self.data = data
        self.count = len(data)

    def execute(self):
        return self


class _CountQuery:
    def __init__(self, rows):
        self.rows = rows
        self.f: list = []

    def select(self, *cols, count=None):
        return self

    def eq(self, c, v):
        self.f.append((c, "eq", v))
        return self

    def gte(self, c, v):
        self.f.append((c, "gte", v))
        return self

    def lt(self, c, v):
        self.f.append((c, "lt", v))
        return self

    def execute(self):
        return _CountResp([r for r in self.rows if all(self._m(r, x) for x in self.f)])

    def _m(self, r, f):
        c, op, v = f
        rv = r.get(c)
        if op == "eq":
            return rv == v
        if op == "gte":
            return rv is not None and rv >= v
        return rv is not None and rv < v  # lt


class _CountSB:
    """count='exact' 를 지원하는 fake — motion_clip_system_exclusions 집계 검증용."""

    def __init__(self, rows):
        self.rows = rows

    def table(self, _t):
        return _CountQuery(self.rows)


def test_aggregate_daily_uses_kst_day_boundary():
    # KST 2026-07-25 [00:00,24:00) = UTC [2026-07-24T15:00, 2026-07-25T15:00).
    rows = [
        {"clip_id": "c1", "state": "candidate"},
        {"clip_id": "c2", "state": "candidate"},
        {"clip_id": "q1", "state": "quarantined"},
        {"clip_id": "b1", "state": "deletion_blocked"},
        # 오늘(KST) 복구 → 카운트, 어제 복구 → 제외.
        {"clip_id": "r1", "state": "restored", "restored_at": "2026-07-25T02:00:00+00:00"},
        {"clip_id": "r0", "state": "restored", "restored_at": "2026-07-24T10:00:00+00:00"},
        # 오늘(KST) 삭제 → 카운트, 어제 삭제 → 제외.
        {"clip_id": "d1", "state": "media_deleted", "media_deleted_at": "2026-07-25T03:00:00+00:00"},
        {"clip_id": "d0", "state": "media_deleted", "media_deleted_at": "2026-07-24T08:00:00+00:00"},
    ]
    agg = aggregate_short_clip_daily(_CountSB(rows), now=NOW)
    assert agg["candidate"] == 2
    assert agg["quarantined"] == 1
    assert agg["review_pending"] == 1        # deletion_blocked
    assert agg["blocked"] == 1
    assert agg["pending_delete"] == 1        # quarantined 대기
    assert agg["restored"] == 1              # 오늘 KST 만
    assert agg["deleted"] == 1               # 오늘 KST 만


def test_aggregate_daily_db_error_hides_raw():
    class _BoomSB:
        def table(self, _t):
            raise RuntimeError("password=hunter2")

    with pytest.raises(ShortClipStoreError) as ei:
        aggregate_short_clip_daily(_BoomSB(), now=NOW)
    assert "hunter2" not in str(ei.value)


class _Resp:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class _RpcSB:
    """rpc(name, args) 를 기록하고 configured data 를 돌려주는 fake. Exception 이면 raise."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name, args):
        self.calls.append((name, args))
        resp = self.responses.get(name)
        if isinstance(resp, Exception):
            raise resp
        return _Resp(resp(args) if callable(resp) else resp)


# ── list: cursor/limit + list|object 정규화 ──
def test_list_candidates_passes_cursor_and_limit_and_normalizes_list():
    rows = [
        {"clip_id": "c1", "started_at": "t1", "duration_sec": 3.6, "displayed_duration_sec": 4},
        {"clip_id": "c2", "started_at": "t2", "duration_sec": 11.4, "displayed_duration_sec": 11},
    ]
    sb = _RpcSB({"fn_list_short_clip_detection_candidates": rows})
    out = list_detection_candidates(
        sb, candidate_under_sec=15, cursor=("t0", "id0"), limit=100
    )
    assert [c.clip_id for c in out] == ["c1", "c2"]
    name, args = sb.calls[0]
    assert name == "fn_list_short_clip_detection_candidates"
    assert args == {
        "p_candidate_under_sec": 15,
        "p_cursor_started_at": "t0",
        "p_cursor_id": "id0",
        "p_limit": 100,
    }


def test_list_candidates_normalizes_single_object_and_none():
    sb = _RpcSB(
        {"fn_list_short_clip_detection_candidates": {"clip_id": "c1", "started_at": "t", "duration_sec": 4.0, "displayed_duration_sec": 4}}
    )
    assert len(list_detection_candidates(sb, candidate_under_sec=15, cursor=None, limit=10)) == 1
    sb2 = _RpcSB({"fn_list_short_clip_detection_candidates": None})
    assert list_detection_candidates(sb2, candidate_under_sec=15, cursor=None, limit=10) == []


def test_list_candidates_null_cursor_sends_both_null():
    sb = _RpcSB({"fn_list_short_clip_detection_candidates": []})
    list_detection_candidates(sb, candidate_under_sec=15, cursor=None, limit=10)
    _, args = sb.calls[0]
    assert args["p_cursor_started_at"] is None and args["p_cursor_id"] is None


# ── record: route 계약 + write flag ──
def test_record_detection_returns_route():
    sb = _RpcSB({"fn_record_short_clip_detection": [{"route": "quarantined", "exclusion_id": "e", "resulting_state": "quarantined"}]})
    res = record_detection(sb, clip_id="c1", now=NOW, write=True)
    assert res.route == "quarantined"
    _, args = sb.calls[0]
    assert args == {"p_clip_id": "c1", "p_now": NOW.isoformat(), "p_write": True}


def test_record_detection_rejects_unknown_route():
    sb = _RpcSB({"fn_record_short_clip_detection": [{"route": "auto_moving"}]})
    with pytest.raises(ValueError):
        record_detection(sb, clip_id="c1", now=NOW, write=False)


# ── claim media deletions: only exclusion/clip/r2_key/lease_token ──
def test_claim_media_deletions_parses_claims():
    rows = [{"exclusion_id": "e1", "clip_id": "cl1", "r2_key": "terra-clips/clips/a.mp4", "lease_token": "t1"}]
    sb = _RpcSB({"fn_claim_short_clip_media_deletions": rows})
    claims = claim_media_deletions(sb, limit=30, worker_host="mac", now=NOW)
    assert claims[0].exclusion_id == "e1" and claims[0].clip_id == "cl1"
    _, args = sb.calls[0]
    assert args == {"p_limit": 30, "p_worker_host": "mac", "p_now": NOW.isoformat()}


# ── complete: false = 실패(성공 아님) ──
def test_complete_true_ok_false_raises_stale():
    sb = _RpcSB({"fn_complete_short_clip_media_delete": True})
    complete_media_delete(sb, exclusion_id="e", lease_token="t", fingerprint="a" * 64, now=NOW)
    _, args = sb.calls[0]
    assert args == {"p_exclusion_id": "e", "p_lease_token": "t", "p_result_fingerprint": "a" * 64, "p_now": NOW.isoformat()}
    sb2 = _RpcSB({"fn_complete_short_clip_media_delete": False})
    with pytest.raises(StaleShortClipError):
        complete_media_delete(sb2, exclusion_id="e", lease_token="t", fingerprint="a" * 64, now=NOW)


def test_complete_rejects_non_lowercase_sha256_fingerprint():
    sb = _RpcSB({"fn_complete_short_clip_media_delete": True})
    for bad in ("ABCDEF", "z" * 64, "a" * 63, "A" * 64):
        with pytest.raises(ValueError):
            complete_media_delete(sb, exclusion_id="e", lease_token="t", fingerprint=bad, now=NOW)
    assert sb.calls == []  # RPC 도달 전에 거부


# ── fail: (exclusion_id, lease_token, allowlisted_code, now) — fingerprint 인자 없음 ──
def test_fail_signature_has_no_fingerprint_and_allowlists_code():
    sb = _RpcSB({"fn_fail_short_clip_media_delete": True})
    fail_media_delete(sb, exclusion_id="e", lease_token="t", code="r2_delete_failed", now=NOW)
    name, args = sb.calls[0]
    assert name == "fn_fail_short_clip_media_delete"
    assert set(args) == {"p_exclusion_id", "p_lease_token", "p_result_code", "p_now"}
    assert "fingerprint" not in "".join(args).lower()
    # 미허용 코드는 RPC 도달 전에 거부.
    with pytest.raises(ValueError):
        fail_media_delete(sb, exclusion_id="e", lease_token="t", code="raw_boom", now=NOW)


def test_fail_false_is_not_success():
    sb = _RpcSB({"fn_fail_short_clip_media_delete": False})
    with pytest.raises(StaleShortClipError):
        fail_media_delete(sb, exclusion_id="e", lease_token="t", code="r2_delete_failed", now=NOW)


# ── Slack notification 3종 signature ──
def test_notification_claim_complete_release_signatures():
    d = date(2026, 7, 25)
    sb = _RpcSB(
        {
            "fn_claim_short_clip_retention_notification": "tok-1",
            "fn_complete_short_clip_retention_notification": True,
            "fn_release_short_clip_retention_notification": True,
        }
    )
    assert claim_retention_notification(sb, summary_date_kst=d, worker_host="mac", now=NOW) == "tok-1"
    assert complete_retention_notification(sb, summary_date_kst=d, claim_token="tok-1", now=NOW) is True
    assert release_retention_notification(sb, summary_date_kst=d, claim_token="tok-1") is True
    names = [c[0] for c in sb.calls]
    assert names == [
        "fn_claim_short_clip_retention_notification",
        "fn_complete_short_clip_retention_notification",
        "fn_release_short_clip_retention_notification",
    ]
    assert sb.calls[0][1] == {"p_summary_date_kst": d.isoformat(), "p_worker_host": "mac", "p_now": NOW.isoformat()}
    assert sb.calls[2][1] == {"p_summary_date_kst": d.isoformat(), "p_claim_token": "tok-1"}


def test_notification_claim_none_when_already_sent():
    sb = _RpcSB({"fn_claim_short_clip_retention_notification": None})
    assert claim_retention_notification(sb, summary_date_kst=date(2026, 7, 25), worker_host="mac", now=NOW) is None


# ── DB error → 안정 내부코드, raw 원문 없음 ──
def test_db_error_hides_raw_message():
    boom = RuntimeError("password=hunter2 host=db.internal")
    sb = _RpcSB({"fn_record_short_clip_detection": boom})
    with pytest.raises(ShortClipStoreError) as ei:
        record_detection(sb, clip_id="c1", now=NOW, write=True)
    msg = str(ei.value)
    assert "hunter2" not in msg and "db.internal" not in msg
    assert "RuntimeError" in msg  # 타입만 노출
