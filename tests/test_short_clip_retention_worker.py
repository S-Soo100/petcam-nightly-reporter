"""짧은 영상 retention metadata worker 안전 계약 테스트 (설계 §3·§5). 실 DB/R2/Slack 무의존.

detection 경로는 metadata-only: download/OpenCV/Gate/detector/model/VLM 호출 0. switch·host guard·
lock·격리·집계 계약을 고정한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import hashlib

from reporter import short_clip_retention_worker as worker
from reporter.short_clip_retention_models import DeletionClaim, DetectionResult, ShortClipCandidate
from reporter.short_clip_retention_store import ShortClipStoreError, StaleShortClipError

NOW = datetime(2026, 7, 25, 0, 0, 0, tzinfo=timezone.utc)  # = 2026-07-25 09:00 KST


def _dc(exclusion="e1", clip="c1", key="terra-clips/clips/a.mp4", token="t1"):
    return DeletionClaim.from_row(
        {"exclusion_id": exclusion, "clip_id": clip, "r2_key": key, "lease_token": token}
    )


class _Lock:
    def __init__(self, granted=True):
        self.granted = granted
        self.released = False


def _acquire(granted=True):
    lock = _Lock(granted)
    return (lambda: (lock if granted else None)), (lambda l: setattr(lock, "released", True)), lock


def _cand(clip_id, started_at="2026-07-20T00:00:01+00:00"):
    return ShortClipCandidate(clip_id=clip_id, started_at=started_at, duration_sec=4.0, displayed_duration_sec=4)


class _RecordFake:
    """record_fn 대역: clip_id → route 매핑. 호출 기록."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, sb, *, clip_id, now, write):
        self.calls.append({"clip_id": clip_id, "write": write})
        route = self.routes.get(clip_id, "candidate")
        return DetectionResult.from_row({"route": route})


def _run(**over):
    """기본 안전 인자로 run 호출(한 페이지 후 종료)."""
    cands = over.pop("candidates", [_cand("c1")])
    pages = over.pop("pages", None)

    def list_fn(sb, *, candidate_under_sec, cursor, limit):
        if pages is not None:
            return pages.pop(0) if pages else []
        return cands if cursor is None else []

    kwargs = dict(
        sb=object(),
        now=NOW,
        enabled=True,
        write_enabled=False,
        expected_host="mac",
        hostname="mac",
        list_candidates_fn=list_fn,
        record_fn=over.pop("record_fn", _RecordFake({})),
        create_client_fn=over.pop("create_client_fn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("create_client called"))),
        batch_limit=over.pop("batch_limit", 100),
    )
    acq, rel, lock = _acquire(over.pop("lock_granted", True))
    kwargs["acquire_lock_fn"] = over.pop("acquire_lock_fn", acq)
    kwargs["release_lock_fn"] = rel
    kwargs.update(over)
    rc = worker.run(**kwargs)
    return rc, lock


# ── disabled → DB client/lock/R2/Slack 0 ──
def test_disabled_touches_nothing():
    created = []
    acq_called = []
    rc = worker.run(
        enabled=False,
        create_client_fn=lambda *a, **k: created.append(1),
        acquire_lock_fn=lambda: acq_called.append(1),
        list_candidates_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("listed")),
    )
    assert rc == 0
    assert created == [] and acq_called == []


# ── blank/mismatched host → nonzero before lock/DB ──
def test_blank_expected_host_fails_closed_before_lock():
    acq_called = []
    rc = worker.run(
        enabled=True, expected_host="", hostname="mac",
        acquire_lock_fn=lambda: acq_called.append(1) or object(),
        create_client_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("sb")),
        list_candidates_fn=lambda *a, **k: [],
    )
    assert rc == 1
    assert acq_called == []


def test_mismatched_host_fails_closed_before_lock():
    acq_called = []
    rc = worker.run(
        enabled=True, expected_host="mac-mini", hostname="someone-macbook",
        acquire_lock_fn=lambda: acq_called.append(1) or object(),
        create_client_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("sb")),
        list_candidates_fn=lambda *a, **k: [],
    )
    assert rc == 1
    assert acq_called == []


# ── lock loser → clean no-op ──
def test_lock_loser_is_noop():
    listed = []
    rc = worker.run(
        enabled=True, expected_host="mac", hostname="mac",
        acquire_lock_fn=lambda: None,
        release_lock_fn=lambda l: None,
        create_client_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("sb")),
        list_candidates_fn=lambda *a, **k: listed.append(1),
    )
    assert rc == 0
    assert listed == []


# ── shadow → write=False, write switch → True ──
def test_shadow_records_with_write_false():
    rec = _RecordFake({"c1": "quarantined"})
    rc, lock = _run(write_enabled=False, record_fn=rec)
    assert rc == 0
    assert rec.calls[0]["write"] is False
    assert lock.released is True


def test_write_switch_records_with_write_true():
    rec = _RecordFake({"c1": "quarantined"})
    rc, _ = _run(write_enabled=True, record_fn=rec)
    assert rc == 0
    assert rec.calls[0]["write"] is True


# ── duplicate reused = success ──
def test_duplicate_reused_is_success():
    rec = _RecordFake({"c1": "reused", "c2": "reused_restored"})
    rc, _ = _run(candidates=[_cand("c1"), _cand("c2")], record_fn=rec)
    assert rc == 0


# ── malformed record route isolated + counted (cycle still ok) ──
def test_malformed_route_is_isolated_and_counted():
    class _BadRecord:
        def __init__(self):
            self.calls = []

        def __call__(self, sb, *, clip_id, now, write):
            self.calls.append(clip_id)
            if clip_id == "bad":
                raise ValueError("unknown detection route: 'auto_p0'")
            return DetectionResult.from_row({"route": "candidate"})

    rec = _BadRecord()
    rc, _ = _run(candidates=[_cand("bad"), _cand("good")], record_fn=rec)
    assert rc == 0  # 격리 — 한 clip 실패가 batch 를 죽이지 않는다
    assert rec.calls == ["bad", "good"]  # 뒤 clip 계속 처리


# ── candidate-list / DB-wide error → nonzero ──
def test_db_wide_list_error_returns_nonzero():
    def boom_list(sb, *, candidate_under_sec, cursor, limit):
        raise ShortClipStoreError("rpc failed: fn_list (TimeoutError)")

    rc, lock = _run(list_candidates_fn=boom_list)
    assert rc == 1
    assert lock.released is True  # 실패해도 lock 해제


def test_record_db_error_returns_nonzero():
    def boom_record(sb, *, clip_id, now, write):
        raise ShortClipStoreError("rpc failed: fn_record (APIError)")

    rc, _ = _run(candidates=[_cand("c1")], record_fn=boom_record)
    assert rc == 1


# ── pagination: 여러 페이지를 keyset 으로 소진 ──
def test_pagination_drains_pages():
    rec = _RecordFake({})
    rc, _ = _run(
        pages=[[_cand("a"), _cand("b")], [_cand("c")], []],
        batch_limit=2,
        record_fn=rec,
    )
    assert rc == 0
    assert {c["clip_id"] for c in rec.calls} == {"a", "b", "c"}


# ── detection 경로 정적 계약: download/OpenCV/Gate/detector/model/VLM 소비 0 ──
# (vlm_host_guard 는 여러 worker 가 공유하는 host guard 이지 VLM 호출이 아니므로 제외 대상 아님.)
def test_worker_module_has_no_media_or_model_symbols():
    import inspect

    src = inspect.getsource(worker)
    for forbidden in (
        "download_clip",
        "cv2",
        "gate_runner",
        "load_detector",
        "assess_clip",
        "sample_frames",
        "anthropic",
        "claude",
        "vlm_selector",
        "vlm_candidate",
        "vlm_models",
        "vlm_store",
        "vlm_backfill",
        "vlm_rolling",
    ):
        assert forbidden not in src, forbidden


# ══════════════════════════════════════════════════════════════════════
# Task 3 — delete cycle + durable Slack
# ══════════════════════════════════════════════════════════════════════
def _delete_cycle(**over):
    kwargs = dict(
        now=NOW,
        worker_host="mac",
        limit=30,
        claim_fn=over.pop("claim_fn", lambda sb, *, limit, worker_host, now: []),
        delete_object_fn=over.pop("delete_object_fn", lambda k: None),
        complete_fn=over.pop("complete_fn", lambda sb, *, exclusion_id, lease_token, fingerprint, now: None),
        fail_fn=over.pop("fail_fn", lambda sb, *, exclusion_id, lease_token, code, now: None),
    )
    kwargs.update(over)
    return worker.run_delete_cycle(object(), **kwargs)


def test_delete_cycle_success_completes_with_key_fingerprint():
    key = "terra-clips/clips/a.mp4"
    completed, deleted = [], []
    stats = _delete_cycle(
        claim_fn=lambda sb, *, limit, worker_host, now: [_dc(key=key)],
        delete_object_fn=lambda k: deleted.append(k),
        complete_fn=lambda sb, *, exclusion_id, lease_token, fingerprint, now: completed.append(fingerprint),
        fail_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("fail called on success")),
    )
    assert deleted == [key]
    assert completed == [hashlib.sha256(key.encode()).hexdigest()]
    assert completed[0] == completed[0].lower() and len(completed[0]) == 64
    assert stats["deleted"] == 1 and stats["audit_divergence"] == 0


def test_delete_cycle_claim_empty_no_r2():
    calls = []
    stats = _delete_cycle(
        claim_fn=lambda sb, *, limit, worker_host, now: [],
        delete_object_fn=lambda k: calls.append(k),
    )
    assert calls == [] and stats["deleted"] == 0 and stats["claimed"] == 0


def test_delete_cycle_r2_failure_fails_once_and_continues():
    failed, completed, del_calls = [], [], []

    def del_fn(k):
        del_calls.append(k)
        if k == "terra-clips/clips/bad.mp4":
            raise RuntimeError("connreset secret://pw@host")

    stats = _delete_cycle(
        claim_fn=lambda sb, *, limit, worker_host, now: [
            _dc(exclusion="e1", key="terra-clips/clips/bad.mp4"),
            _dc(exclusion="e2", key="terra-clips/clips/good.mp4"),
        ],
        delete_object_fn=del_fn,
        complete_fn=lambda sb, *, exclusion_id, lease_token, fingerprint, now: completed.append(exclusion_id),
        fail_fn=lambda sb, *, exclusion_id, lease_token, code, now: failed.append((exclusion_id, code)),
    )
    assert failed == [("e1", "r2_delete_failed")]  # allowlist code, raw 예외 없음
    assert completed == ["e2"]                      # 실패해도 다음 object 계속
    assert stats["delete_failed"] == 1 and stats["deleted"] == 1


def test_delete_cycle_complete_false_is_audit_divergence_and_aborts():
    deleted = []

    def complete_stale(sb, *, exclusion_id, lease_token, fingerprint, now):
        raise StaleShortClipError("stale complete")

    stats = _delete_cycle(
        claim_fn=lambda sb, *, limit, worker_host, now: [
            _dc(exclusion="e1", key="terra-clips/clips/a.mp4"),
            _dc(exclusion="e2", key="terra-clips/clips/b.mp4"),
        ],
        delete_object_fn=lambda k: deleted.append(k),
        complete_fn=complete_stale,
    )
    assert stats["audit_divergence"] == 1
    assert len(deleted) == 1  # divergence 후 cycle abort — 뒤 claim 처리 안 함(성공 계속 주장 금지)


def test_delete_disabled_makes_no_claim_or_r2():
    claim_called, del_called = [], []
    rc, _ = _run(
        delete_enabled=False,
        claim_deletions_fn=lambda sb, *, limit, worker_host, now: claim_called.append(1) or [],
        delete_object_fn=lambda k: del_called.append(k),
        claim_notification_fn=lambda sb, *, summary_date_kst, worker_host, now: None,
    )
    assert rc == 0 and claim_called == [] and del_called == []


def test_delete_audit_divergence_makes_run_nonzero():
    def complete_stale(sb, *, exclusion_id, lease_token, fingerprint, now):
        raise StaleShortClipError("stale")

    rc, _ = _run(
        delete_enabled=True,
        claim_deletions_fn=lambda sb, *, limit, worker_host, now: [_dc()],
        delete_object_fn=lambda k: None,
        complete_delete_fn=complete_stale,
        fail_delete_fn=lambda *a, **k: None,
        claim_notification_fn=lambda sb, *, summary_date_kst, worker_host, now: None,
    )
    assert rc == 1


# ── durable Slack: report hour gate + claim→complete/release ──
def _run_slack(**over):
    posted, completed, released = [], [], []
    claim_ret = over.pop("claim_ret", "tok-1")
    post_ok = over.pop("post_ok", True)
    rc, _ = _run(
        now=over.pop("now", NOW),  # 09:00 KST
        report_hour=over.pop("report_hour", 9),
        claim_notification_fn=lambda sb, *, summary_date_kst, worker_host, now: (
            posted.append(("claim", summary_date_kst)) or claim_ret
        ),
        post_slack_fn=lambda text: (posted.append(("post", text)) or post_ok),
        complete_notification_fn=lambda sb, *, summary_date_kst, claim_token, now: completed.append(claim_token) or True,
        release_notification_fn=lambda sb, *, summary_date_kst, claim_token: released.append(claim_token) or True,
        **over,
    )
    return rc, posted, completed, released


def test_slack_after_report_hour_claims_and_completes_on_success():
    rc, posted, completed, released = _run_slack(post_ok=True)
    kinds = [p[0] for p in posted]
    assert "claim" in kinds and "post" in kinds
    assert completed == ["tok-1"] and released == []
    # 카드에 raw key/token/fingerprint/URL 없음.
    posted_text = next(p[1] for p in posted if p[0] == "post")
    for leak in ("terra-clips", "tok-1", "https://", "cloudflarestorage"):
        assert leak not in posted_text


def test_slack_failure_releases_claim():
    rc, posted, completed, released = _run_slack(post_ok=False)
    assert completed == [] and released == ["tok-1"]


def test_slack_noop_before_report_hour():
    # 05:00 KST = 2026-07-24 20:00 UTC (report_hour 9 이전) → claim/post 0.
    before = datetime(2026, 7, 24, 20, 0, 0, tzinfo=timezone.utc)
    rc, posted, completed, released = _run_slack(now=before, report_hour=9)
    assert posted == [] and completed == [] and released == []


def test_slack_claim_none_sends_nothing():
    rc, posted, completed, released = _run_slack(claim_ret=None)
    assert [p[0] for p in posted] == ["claim"]  # claim 시도만, post 없음
    assert completed == [] and released == []
