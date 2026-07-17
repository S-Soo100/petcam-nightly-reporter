"""역사 motion_clips 를 Python evidence durable queue 에 bounded 하게 enqueue.

**영상 다운로드/분석 0** — DB 만 건드린다(설계 §10). 신규 clip 은 motion_clips trigger 가 live job 을
자동 생성하지만, trigger 도입 이전의 과거 clip 은 이 스크립트가 날짜 범위·batch 단위로 넣는다.

원칙:
  - `--start-date`/`--end-date`/`--limit` **필수**(무한 backfill 기본값 금지).
  - source='historical', priority=10(live 100 보다 낮음 → live 우선, 남는 capacity 만).
  - 같은 clip 중복 job 0: unique(clip_id, schema, algorithm) on-conflict-do-nothing.
  - `--dry-run` 은 조회만, mutation 0.
  - started_at → id 안정 정렬 pagination, --limit 로 bound.

    uv run python -m scripts.enqueue_python_evidence_backfill \
      --start-date 2026-07-01 --end-date 2026-07-08 --limit 500 [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone

from gecko_vision_gate.temporal_evidence import ALGORITHM_VERSION, EVIDENCE_SCHEMA_VERSION

_JOBS_CONFLICT = "clip_id,evidence_schema_version,algorithm_version"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Python evidence 역사 backfill enqueuer (DB only)")
    p.add_argument("--start-date", required=True, type=_date, help="시작일(포함) YYYY-MM-DD")
    p.add_argument("--end-date", required=True, type=_date, help="종료일(포함) YYYY-MM-DD")
    p.add_argument("--limit", required=True, type=int, help="이번 실행 최대 clip 수(무한 금지)")
    p.add_argument("--dry-run", action="store_true", help="조회만, mutation 0")
    return p.parse_args(argv)


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _fetch_clips(sb, start_date: date, end_date: date, limit: int, page_size: int) -> list[dict]:
    """[start_date 00:00, end_date+1day 00:00) 창의 clip 을 started_at→id 안정정렬로 최대 limit 개."""
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_excl = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        take = min(page_size, limit - len(out))
        page = (
            sb.table("motion_clips")
            .select("id, started_at")
            .gte("started_at", start.isoformat())
            .lt("started_at", end_excl.isoformat())
            # H5: supabase-py .order 는 컬럼 1개만 positional — 연속 호출로 (started_at, id) 안정 정렬.
            .order("started_at")
            .order("id")
            .range(offset, offset + take - 1)
            .execute()
            .data
        ) or []
        out.extend(page)
        if len(page) < take:
            break  # 더 없음
        offset += take
    return out[:limit]


# 한 실행 안전 상한(폭주/실수 방지). 더 필요하면 여러 번 나눠 돌린다(stop/resume 가능).
_LIMIT_SAFE_CAP = 5000


def enqueue_backfill(sb, *, start_date: date, end_date: date, limit: int,
                     dry_run: bool, page_size: int = 1000) -> dict:
    """날짜 범위 clip 을 historical job 으로 enqueue(중복 no-op). dry-run 이면 mutation 0."""
    if limit < 1 or limit > _LIMIT_SAFE_CAP:
        raise ValueError(f"limit must be 1..{_LIMIT_SAFE_CAP} (got {limit})")
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")
    clips = _fetch_clips(sb, start_date, end_date, limit, page_size)
    if dry_run:
        return {"scanned": len(clips), "enqueued": 0, "dry_run": True}
    rows = [
        {
            "clip_id": c["id"],
            "source": "historical",
            "priority": 10,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
        }
        for c in clips
    ]
    enqueued = 0
    if rows:
        inserted = (
            sb.table("python_evidence_jobs")
            .upsert(rows, on_conflict=_JOBS_CONFLICT, ignore_duplicates=True)
            .execute()
            .data
        ) or []
        enqueued = len(inserted)
    return {"scanned": len(clips), "enqueued": enqueued, "dry_run": False}


def main(argv=None) -> int:
    from supabase import create_client

    from reporter import config

    ns = parse_args(argv)
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    stats = enqueue_backfill(sb, start_date=ns.start_date, end_date=ns.end_date,
                             limit=ns.limit, dry_run=ns.dry_run)
    print(f"[pyevidence-backfill] {ns.start_date}..{ns.end_date} "
          f"scanned={stats['scanned']} enqueued={stats['enqueued']} dry_run={stats['dry_run']}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
