"""역사 motion_clips 를 Python evidence durable queue 에 bounded 하게 enqueue.

**영상 다운로드/분석 0** — DB 만 건드린다(설계 §10).

B1R2: 날짜 추정 대신 **R2 가용성 검증 private manifest**(media_available_silent allowlist)에서만 enqueue.
날짜 범위 backfill(`enqueue_backfill`)은 함수로 보존하되, CLI 는 manifest-bound 로 전환했다. 소실된 원본을
반복 재큐하던 B1R 문제를 원천 차단한다(R2 object 존재가 검증된 clip 만 입력).

원칙:
  - `--availability-manifest`/`--expected-manifest-sha`/`--limit` **필수**(무한 backfill 기본값 금지).
  - manifest SHA(정렬된 clip_id\\tstatus)를 전송 무결성으로 검증 — 불일치면 partial enqueue 없이 즉시 실패.
  - `--required-status media_available_silent` 만 허용(design §8). 비-silent row 는 절대 enqueue 안 함.
  - source='historical', priority=10(live 100 보다 낮음 → live 우선, 남는 capacity 만).
  - 같은 clip 중복 job 0: unique(clip_id, schema, algorithm) on-conflict-do-nothing.
  - `--dry-run` 은 조회만, mutation 0.
  - **missing-progress**: 이미 active identity job 이 있는 clip 은 건너뛰고 다음 missing 으로 전진(굶김 방지).

    uv run python -m scripts.enqueue_python_evidence_backfill \
      --availability-manifest /abs/private/canary.jsonl \
      --expected-manifest-sha <64-hex> --required-status media_available_silent \
      --limit 30 [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from gecko_vision_gate.temporal_evidence import ALGORITHM_VERSION, EVIDENCE_SCHEMA_VERSION

_JOBS_CONFLICT = "clip_id,evidence_schema_version,algorithm_version"


def parse_args(argv=None) -> argparse.Namespace:
    # B1R2: 날짜 범위 대신 R2 가용성 검증 private manifest(media_available_silent allowlist)로 enqueue.
    p = argparse.ArgumentParser(description="Python evidence manifest-bound backfill enqueuer (DB only)")
    p.add_argument("--availability-manifest", required=True, help="R2 가용 private JSONL(절대경로)")
    p.add_argument("--expected-manifest-sha", required=True,
                   help="manifest (clip_id,status) canonical SHA-256 (64 hex, 전송 무결성 검증)")
    p.add_argument("--required-status", default="media_available_silent",
                   choices=["media_available_silent"],
                   help="enqueue 대상 상태. design §8: silent 만 backfill 입력")
    p.add_argument("--limit", required=True, type=int, help="이번 실행 최대 enqueue 수(무한 금지)")
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


def _read_manifest(path) -> list[dict]:
    rows: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _manifest_sha256(rows: list[dict]) -> str:
    """audit 의 availability_sha256 과 동치: 정렬된 (clip_id, status) 쌍 (전송 무결성 검증용)."""
    payload = "\n".join(
        f"{r['clip_id']}\t{r['status']}" for r in sorted(rows, key=lambda x: x["clip_id"])
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def enqueue_from_manifest(sb, manifest_path, *, expected_sha: str,
                          required_status: str = "media_available_silent",
                          limit: int, dry_run: bool) -> dict:
    """R2 가용성 검증 manifest 의 media_available_silent clip 만 historical job 으로 enqueue.

    - SHA 불일치(전송 오염/잘못된 manifest) → 즉시 ValueError. partial enqueue 안 함.
    - required_status(=media_available_silent) 아닌 row 는 절대 enqueue 하지 않는다(design §8 allowlist).
    - 이미 active identity job 이 있는 clip 은 건너뛰고 다음 missing 으로 전진(중복 0, 굶김 방지).
    - dry-run 은 selected 만 세고 mutation 0.
    """
    if limit < 1 or limit > _LIMIT_SAFE_CAP:
        raise ValueError(f"limit must be 1..{_LIMIT_SAFE_CAP} (got {limit})")
    if required_status != "media_available_silent":
        raise ValueError(f"required_status must be media_available_silent (got {required_status!r})")

    rows = _read_manifest(manifest_path)
    actual_sha = _manifest_sha256(rows)
    if actual_sha != expected_sha:
        raise ValueError(f"manifest_sha_mismatch: expected={expected_sha[:12]} actual={actual_sha[:12]}")

    candidate_ids = [r["clip_id"] for r in rows if r.get("status") == required_status]
    existing = _load_existing_job_clip_ids(sb, candidate_ids)
    missing = [cid for cid in sorted(dict.fromkeys(candidate_ids)) if cid not in existing]
    selected = missing[:limit]

    base = {"manifest_total": len(rows), "required_status": required_status,
            "candidates": len(candidate_ids), "selected": len(selected)}
    if dry_run:
        return {**base, "enqueued": 0, "dry_run": True}

    insert_rows = [
        {"clip_id": cid, "source": "historical", "priority": 10,
         "evidence_schema_version": EVIDENCE_SCHEMA_VERSION, "algorithm_version": ALGORITHM_VERSION}
        for cid in selected
    ]
    enqueued = 0
    if insert_rows:
        inserted = (
            sb.table("python_evidence_jobs")
            .upsert(insert_rows, on_conflict=_JOBS_CONFLICT, ignore_duplicates=True)
            .execute()
            .data
        ) or []
        enqueued = len(inserted)
    return {**base, "enqueued": enqueued, "dry_run": False}


def main(argv=None) -> int:
    from supabase import create_client

    from reporter import config

    ns = parse_args(argv)
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    stats = enqueue_from_manifest(
        sb, ns.availability_manifest, expected_sha=ns.expected_manifest_sha,
        required_status=ns.required_status, limit=ns.limit, dry_run=ns.dry_run,
    )
    print(f"[pyevidence-backfill] manifest={ns.availability_manifest} "
          f"status={stats['required_status']} candidates={stats['candidates']} "
          f"selected={stats['selected']} enqueued={stats['enqueued']} dry_run={stats['dry_run']}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
