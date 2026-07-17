"""enqueue_python_evidence_backfill — 역사 영상 bounded enqueuer.

영상 다운로드/분석 0. 날짜 범위 + --limit 필수(무한 기본 금지), 안정 date/id pagination,
live job 충돌 시 no-op(중복 job 0), dry-run mutation 0 을 고정한다. DB 무의존(fake).
"""

from __future__ import annotations

from datetime import date

import pytest

from scripts import enqueue_python_evidence_backfill as bf
from reporter.python_evidence_store import ALLOWED_SOURCE


class _Result:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class FakeJobsSB:
    """motion_clips 조회 + python_evidence_jobs upsert(on-conflict-do-nothing) 모사."""

    def __init__(self, clips, existing_job_clip_ids=()):
        self._clips = clips  # [{"id","started_at"}]
        self.jobs = [{"clip_id": c, "evidence_schema_version": "python-evidence-raw-v1",
                      "algorithm_version": "croi-temporal-v1"} for c in existing_job_clip_ids]
        self.insert_calls = 0

    # motion_clips 조회 체인
    def table(self, name):
        return _Query(name, self)


class _Query:
    def __init__(self, name, sb):
        self._name = name
        self._sb = sb
        self._filters = []
        self._order = None
        self._range = None
        self._pending = None
        self._ignore = False

    def select(self, *_c):
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def lt(self, col, val):
        self._filters.append(("lt", col, val))
        return self

    def order(self, *cols, **_kw):
        self._order = cols
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False, **_kw):
        self._pending = ("upsert", rows if isinstance(rows, list) else [rows], ignore_duplicates)
        return self

    def execute(self):
        if self._pending and self._pending[0] == "upsert":
            self._sb.insert_calls += 1
            _, rows, ignore = self._pending
            inserted = []
            for r in rows:
                key = (r["clip_id"], r["evidence_schema_version"], r["algorithm_version"])
                exists = any((j["clip_id"], j["evidence_schema_version"], j["algorithm_version"]) == key
                             for j in self._sb.jobs)
                if exists and ignore:
                    continue  # on conflict do nothing
                self._sb.jobs.append(dict(r))
                inserted.append(dict(r))
            return _Result(inserted)
        # motion_clips 조회
        rows = sorted(self._sb._clips, key=lambda c: (c["started_at"], c["id"]))
        if self._range:
            lo, hi = self._range
            rows = rows[lo:hi + 1]
        return _Result(rows)


def _clips(n, day="2026-07-10"):
    return [{"id": f"clip-{i:03d}", "started_at": f"{day}T{i % 24:02d}:00:00+00:00"} for i in range(n)]


# ── CLI 필수 인자 ──

def test_cli_requires_start_end_limit():
    with pytest.raises(SystemExit):
        bf.parse_args([])
    with pytest.raises(SystemExit):
        bf.parse_args(["--start-date", "2026-07-01"])  # end/limit 누락
    with pytest.raises(SystemExit):
        bf.parse_args(["--start-date", "2026-07-01", "--end-date", "2026-07-02"])  # limit 누락


def test_cli_has_no_unbounded_default_limit():
    ns = bf.parse_args(["--start-date", "2026-07-01", "--end-date", "2026-07-02", "--limit", "50"])
    assert ns.limit == 50 and ns.start_date == date(2026, 7, 1) and ns.end_date == date(2026, 7, 2)


# ── enqueue 동작 ──

def test_enqueue_inserts_historical_priority_jobs():
    sb = FakeJobsSB(_clips(5))
    stats = bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                                limit=10, dry_run=False)
    assert stats["scanned"] == 5 and stats["enqueued"] == 5
    assert all(j.get("source") == "historical" and j.get("priority") == 10 for j in sb.jobs)
    assert ALLOWED_SOURCE == frozenset({"live", "historical"})


def test_enqueue_respects_limit():
    sb = FakeJobsSB(_clips(1000))
    stats = bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                                limit=30, dry_run=False, page_size=100)
    assert stats["scanned"] == 30 and stats["enqueued"] == 30


def test_live_job_conflict_is_noop():
    # 이미 (live) job 이 있는 clip → historical enqueue 시 중복 job 0
    sb = FakeJobsSB(_clips(3), existing_job_clip_ids=["clip-000", "clip-001"])
    stats = bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                                limit=10, dry_run=False)
    assert stats["scanned"] == 3
    assert stats["enqueued"] == 1  # clip-002 만 신규
    # 중복 job 이 생기지 않았다
    assert len([j for j in sb.jobs if j["clip_id"] == "clip-000"]) == 1


def test_dry_run_mutates_nothing():
    sb = FakeJobsSB(_clips(5))
    stats = bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                                limit=10, dry_run=True)
    assert stats["scanned"] == 5 and stats["enqueued"] == 0
    assert sb.insert_calls == 0  # mutation 0
    assert sb.jobs == []


def test_enqueue_never_imports_r2_or_compute():
    # 이 스크립트는 영상 다운로드/분석을 하지 않는다 — 모듈에 r2/compute 참조가 없어야 한다.
    import inspect
    src = inspect.getsource(bf)
    assert "download_clip" not in src and "compute_temporal_evidence" not in src
    assert "r2" not in src.replace("r2_key", "")  # r2_key 언급은 허용, r2 모듈 사용은 금지
