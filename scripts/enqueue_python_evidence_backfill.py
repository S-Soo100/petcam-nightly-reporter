"""역사 motion_clips 를 Python evidence durable queue 에 bounded 하게 enqueue.

**영상 다운로드/분석 0** — DB 만 건드린다(설계 §10). 신규 clip 은 motion_clips trigger 가 live job 을
자동 생성하지만, trigger 도입 이전의 과거 clip 은 이 스크립트가 날짜 범위·batch 단위로 넣는다.

원칙:
  - `--start-date`/`--end-date`/`--limit` **필수**(무한 backfill 기본값 금지).
  - source='historical', priority=10(live 100 보다 낮음 → live 우선, 남는 capacity 만).
  - 같은 clip 중복 job 0: unique(clip_id, schema, algorithm) on-conflict-do-nothing.
  - `--dry-run` 은 조회만, mutation 0.
  - **missing-progress**: 범위 전체를 페이지 단위로 순회하며 이미 active job 이 있는 clip 을 건너뛰고
    다음 missing clip 으로 전진한다. 같은 range 를 반복해도 앞쪽 job 에 막혀 굶지 않는다(B1R Task 2).
  - `--cutoff-started-at` 이하 clip 만 역사 분모. 이후 신규 live clip 은 live queue 가 처리.

    uv run python -m scripts.enqueue_python_evidence_backfill \
      --start-date 2026-07-01 --end-date 2026-07-08 --limit 500 \
      [--cutoff-started-at 2026-07-22T02:45:33+00:00] [--dry-run]
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
    p.add_argument("--limit", required=True, type=int, help="이번 실행 최대 missing clip 수(무한 금지)")
    p.add_argument("--cutoff-started-at", default=None,
                   help="이 시각 이하 clip 만 역사 분모(ISO-8601). 미지정 시 날짜 범위 전체.")
    p.add_argument("--dry-run", action="store_true", help="조회만, mutation 0")
    return p.parse_args(argv)


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# 한 실행 안전 상한(폭주/실수 방지). 더 필요하면 여러 번 나눠 돌린다(stop/resume 가능).
_LIMIT_SAFE_CAP = 5000


def _fetch_motion_page(sb, start_iso: str, end_excl_iso: str, cutoff_iso: str | None,
                       offset: int, size: int) -> list[dict]:
    """[start,end_excl) ∩ (started_at<=cutoff) 창을 started_at→id 안정정렬로 offset 페이지 조회."""
    query = (
        sb.table("motion_clips")
        .select("id, started_at")
        .gte("started_at", start_iso)
        .lt("started_at", end_excl_iso)
    )
    if cutoff_iso is not None:
        query = query.lte("started_at", cutoff_iso)
    # H5: supabase-py .order 는 컬럼 1개만 positional — 연속 호출로 (started_at, id) 안정 정렬.
    return (
        query.order("started_at").order("id").range(offset, offset + size - 1).execute().data
    ) or []


def _load_existing_job_clip_ids(sb, clip_ids, *, batch: int = 200) -> set:
    """clip_ids 중 active identity(schema,algo) job 이 이미 있는 clip 집합. in-list 를 batch 로 조회."""
    found: set = set()
    unique = list(dict.fromkeys(cid for cid in clip_ids if cid is not None))
    for i in range(0, len(unique), batch):
        chunk = unique[i : i + batch]
        rows = (
            sb.table("python_evidence_jobs")
            .select("clip_id")
            .in_("clip_id", chunk)
            .eq("evidence_schema_version", EVIDENCE_SCHEMA_VERSION)
            .eq("algorithm_version", ALGORITHM_VERSION)
            .execute()
            .data
        ) or []
        for r in rows:
            found.add(r["clip_id"])
    return found


def load_missing_clips(sb, *, start_date: date, end_date: date, cutoff_started_at: str | None,
                       limit: int, page_size: int = 500) -> list[dict]:
    """범위 전체를 페이지로 순회하며 active job 이 없는 playable clip 만 최대 limit 개 반환.

    v1(`_fetch_clips`)은 앞 limit 개만 읽어 그 앞이 이미 job 으로 차 있으면 뒤쪽 missing clip 으로
    전진하지 못했다(굶김, B1R design §6). 여기서는 page 를 넘기며 기존 job clip 을 건너뛰고 offset 을
    fetch 한 만큼 전진해 다음 missing 으로 나아간다.

    cutoff 가 historical set 을 고정하므로(그 이하 clip 은 started_at 이 변하지 않음) offset pagination
    이 안정적이다 — keyset OR 술어 없이도 누락·중복 0. 반복 호출은 매번 offset 0 에서 시작하지만
    이미 job 이 생긴 앞쪽을 건너뛰어 다음 missing page 에 도달한다.
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1 (got {limit})")
    start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_excl = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        take = min(page_size, limit - len(out))
        page = _fetch_motion_page(
            sb, start.isoformat(), end_excl.isoformat(), cutoff_started_at, offset, take
        )
        if not page:
            break
        existing = _load_existing_job_clip_ids(sb, [row["id"] for row in page])
        out.extend(row for row in page if row["id"] not in existing)
        offset += len(page)
        if len(page) < take:
            break  # 범위 끝
    return out[:limit]


def enqueue_backfill(sb, *, start_date: date, end_date: date, limit: int, dry_run: bool,
                     cutoff_started_at: str | None = None, page_size: int = 1000) -> dict:
    """날짜 범위의 missing clip 을 historical job 으로 enqueue(중복 no-op). dry-run 이면 mutation 0.

    stats["scanned"] = 이번 실행에서 찾은 missing clip 수(=enqueue 대상). enqueued = 실제 신규 insert 수.
    """
    if limit < 1 or limit > _LIMIT_SAFE_CAP:
        raise ValueError(f"limit must be 1..{_LIMIT_SAFE_CAP} (got {limit})")
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")
    missing = load_missing_clips(
        sb, start_date=start_date, end_date=end_date,
        cutoff_started_at=cutoff_started_at, limit=limit, page_size=page_size,
    )
    if dry_run:
        return {"scanned": len(missing), "enqueued": 0, "dry_run": True}
    rows = [
        {
            "clip_id": c["id"],
            "source": "historical",
            "priority": 10,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
        }
        for c in missing
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
    return {"scanned": len(missing), "enqueued": enqueued, "dry_run": False}


def main(argv=None) -> int:
    from supabase import create_client

    from reporter import config

    ns = parse_args(argv)
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    stats = enqueue_backfill(sb, start_date=ns.start_date, end_date=ns.end_date,
                             limit=ns.limit, dry_run=ns.dry_run,
                             cutoff_started_at=ns.cutoff_started_at)
    print(f"[pyevidence-backfill] {ns.start_date}..{ns.end_date} "
          f"cutoff={ns.cutoff_started_at} missing={stats['scanned']} "
          f"enqueued={stats['enqueued']} dry_run={stats['dry_run']}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
