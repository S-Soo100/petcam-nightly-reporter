"""enqueue_python_evidence_backfill — 역사 영상 bounded enqueuer.

영상 다운로드/분석 0. 날짜 범위 + --limit 필수(무한 기본 금지), 안정 date/id pagination,
live job 충돌 시 no-op(중복 job 0), dry-run mutation 0 을 고정한다. DB 무의존(fake).

B1R Task 2: missing-progress 하드닝 — 범위 전체를 순회하며 이미 job 이 있는 clip 을 건너뛰고 다음
missing clip 으로 전진한다(앞쪽 job 에 막혀 굶지 않음). cutoff 이하 clip 만 역사 분모.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from scripts import enqueue_python_evidence_backfill as bf
from reporter.python_evidence_store import ALLOWED_SOURCE

_SCHEMA = "python-evidence-raw-v1"
_ALGO = "croi-temporal-v1"


def _to_dt(v) -> datetime:
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class _Result:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class FakeJobsSB:
    """motion_clips 조회 + python_evidence_jobs select/upsert(on-conflict-do-nothing) 모사."""

    def __init__(self, clips, existing_job_clip_ids=()):
        self._clips = clips  # [{"id","started_at"}]
        self.jobs = [{"clip_id": c, "evidence_schema_version": _SCHEMA,
                      "algorithm_version": _ALGO} for c in existing_job_clip_ids]
        self.insert_calls = 0

    def table(self, name):
        return _Query(name, self)


class _Query:
    def __init__(self, name, sb):
        self._name = name
        self._sb = sb
        self._filters = []
        self._order = []
        self._range = None
        self._pending = None

    def select(self, *_c):
        return self

    def gte(self, col, val):
        self._filters.append(("gte", col, val))
        return self

    def lt(self, col, val):
        self._filters.append(("lt", col, val))
        return self

    def lte(self, col, val):
        self._filters.append(("lte", col, val))
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in_", col, list(vals)))
        return self

    def order(self, column, *, desc=False, nullsfirst=None, foreign_table=None):
        # 실제 supabase-py 시그니처와 동일 — 컬럼 1개만 positional. 잘못된 2번째 positional 은 TypeError.
        self._order.append(column)
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

        if self._name == "python_evidence_jobs":
            # SELECT clip_id — active identity job 존재 조회
            out = []
            for j in self._sb.jobs:
                ok = True
                for kind, col, val in self._filters:
                    if kind == "in_" and j.get(col) not in val:
                        ok = False
                        break
                    if kind == "eq" and j.get(col) != val:
                        ok = False
                        break
                if ok:
                    out.append({"clip_id": j["clip_id"]})
            return _Result(out)

        # motion_clips 조회 — started_at 필터(gte/lt/lte) 적용 후 (started_at,id) 안정정렬 + range slice
        rows = list(self._sb._clips)
        for kind, col, val in self._filters:
            if col != "started_at":
                continue
            bound = _to_dt(val)
            if kind == "gte":
                rows = [c for c in rows if _to_dt(c[col]) >= bound]
            elif kind == "lt":
                rows = [c for c in rows if _to_dt(c[col]) < bound]
            elif kind == "lte":
                rows = [c for c in rows if _to_dt(c[col]) <= bound]
        rows = sorted(rows, key=lambda c: (c["started_at"], c["id"]))
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


def test_cli_accepts_optional_cutoff():
    ns = bf.parse_args(["--start-date", "2026-07-01", "--end-date", "2026-07-02", "--limit", "50",
                        "--cutoff-started-at", "2026-07-22T02:45:33+00:00"])
    assert ns.cutoff_started_at == "2026-07-22T02:45:33+00:00"
    ns2 = bf.parse_args(["--start-date", "2026-07-01", "--end-date", "2026-07-02", "--limit", "50"])
    assert ns2.cutoff_started_at is None


# ── enqueue 동작 ──

def test_enqueue_inserts_historical_priority_jobs():
    sb = FakeJobsSB(_clips(5))
    stats = bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                                limit=10, dry_run=False)
    assert stats["scanned"] == 5 and stats["enqueued"] == 5
    assert all(j.get("source") == "historical" and j.get("priority") == 10
               for j in sb.jobs if "source" in j)
    assert ALLOWED_SOURCE == frozenset({"live", "historical"})


def test_enqueue_respects_limit():
    sb = FakeJobsSB(_clips(1000))
    stats = bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                                limit=30, dry_run=False, page_size=100)
    assert stats["scanned"] == 30 and stats["enqueued"] == 30
    assert len({j["clip_id"] for j in sb.jobs}) == 30


def test_live_job_conflict_is_noop():
    # 이미 (live) job 이 있는 clip → historical enqueue 시 중복 job 0. missing-scan 이라 scanned=missing 수.
    sb = FakeJobsSB(_clips(3), existing_job_clip_ids=["clip-000", "clip-001"])
    stats = bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                                limit=10, dry_run=False)
    assert stats["scanned"] == 1   # clip-002 만 missing
    assert stats["enqueued"] == 1  # clip-002 만 신규
    assert len([j for j in sb.jobs if j["clip_id"] == "clip-000"]) == 1  # 중복 job 안 생김


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


# ── H5: order 는 연속 호출(실제 SDK 시그니처) + 안정 pagination + limit 검증 ──

def test_order_uses_chained_calls_not_second_positional():
    # 실제 supabase-py order 는 컬럼 1개만 받는다. 잘못된 .order("a","b") 였다면 Fake 가 TypeError.
    sb = FakeJobsSB(_clips(3))
    bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                        limit=10, dry_run=True)  # 조회만 — order 호출 경로 통과하면 성공


def test_same_started_at_pagination_no_dup_or_miss():
    # 같은 started_at 이 여러 페이지에 걸쳐도 (started_at,id) 안정정렬 + range offset 으로 누락·중복 0
    clips = [{"id": f"clip-{i:03d}", "started_at": "2026-07-10T00:00:00+00:00"} for i in range(250)]
    sb = FakeJobsSB(clips)
    stats = bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 10),
                                limit=250, dry_run=False, page_size=100)
    assert stats["scanned"] == 250 and stats["enqueued"] == 250
    ids = [j["clip_id"] for j in sb.jobs]
    assert len(ids) == len(set(ids)) == 250  # 중복 0, 누락 0


def test_limit_must_be_positive_and_bounded():
    sb = FakeJobsSB(_clips(3))
    with pytest.raises(ValueError):
        bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                            limit=0, dry_run=True)
    with pytest.raises(ValueError):
        bf.enqueue_backfill(sb, start_date=date(2026, 7, 10), end_date=date(2026, 7, 11),
                            limit=10_000, dry_run=True)  # 안전 상한 초과


# ── B1R Task 2: missing-progress 하드닝 ──
D = date(2026, 7, 10)


def test_existing_first_page_advances_to_later_missing_clips():
    # 앞 100개에 이미 job 이 있어도 뒤쪽 missing 20개로 전진해야 한다(굶김 방지).
    sb = FakeJobsSB(_clips(120), existing_job_clip_ids=[f"clip-{i:03d}" for i in range(100)])
    stats = bf.enqueue_backfill(sb, start_date=D, end_date=D, limit=20, dry_run=False, page_size=25)
    assert stats["enqueued"] == 20
    assert {j["clip_id"] for j in sb.jobs} >= {f"clip-{i:03d}" for i in range(100, 120)}


def test_same_range_second_run_reaches_next_missing_page():
    # 같은 range 를 두 번 돌리면 첫 실행이 채운 앞쪽을 건너뛰고 다음 missing page 에 도달한다.
    sb = FakeJobsSB(_clips(80))
    first = bf.enqueue_backfill(sb, start_date=D, end_date=D, limit=30, dry_run=False, page_size=10)
    second = bf.enqueue_backfill(sb, start_date=D, end_date=D, limit=30, dry_run=False, page_size=10)
    assert (first["enqueued"], second["enqueued"]) == (30, 30)
    assert len({j["clip_id"] for j in sb.jobs}) == 60


def test_cutoff_prevents_new_live_clip_from_historical_enqueue():
    # cutoff 이후 clip 은 historical 분모에서 제외 → enqueue 0.
    cutoff = "2026-07-10T02:00:00+00:00"
    sb = FakeJobsSB([{"id": "live-1", "started_at": "2026-07-10T05:00:00+00:00"}])
    stats = bf.enqueue_backfill(sb, start_date=D, end_date=D, limit=30,
                                cutoff_started_at=cutoff, dry_run=False)
    assert stats["enqueued"] == 0
    assert sb.jobs == []
