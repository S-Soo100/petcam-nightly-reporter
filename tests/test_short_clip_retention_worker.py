"""짧은 영상 retention metadata worker 안전 계약 테스트 (설계 §3·§5). 실 DB/R2/Slack 무의존.

detection 경로는 metadata-only: download/OpenCV/Gate/detector/model/VLM 호출 0. switch·host guard·
lock·격리·집계 계약을 고정한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from reporter import short_clip_retention_worker as worker
from reporter.short_clip_retention_models import DetectionResult, ShortClipCandidate
from reporter.short_clip_retention_store import ShortClipStoreError

NOW = datetime(2026, 7, 25, 0, 0, 0, tzinfo=timezone.utc)


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
