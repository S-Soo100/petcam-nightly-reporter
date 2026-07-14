"""미처리 motion_clips 선택 — allowlist 카메라 + clip_prelabels 에 없는 것.

motion_clips 는 불변(상태 컬럼 금지, 지시문 §438). "처리됨" 은 clip_prelabels 에 같은
(model_version, schema_version) evidence 가 있으면 = 처리됨. 없으면 미처리 → 이번 배치 대상.
같은 버전 재실행은 자연히 0건(멱등), Gate 버전이 바뀌면 전체가 다시 미처리로 잡힌다.
"""

from __future__ import annotations

from datetime import datetime

from reporter.indexer import ClipMeta

_CLIP_FIELDS = ("id", "camera_id", "started_at", "duration_sec", "r2_key", "motion_score")


def list_unprocessed_clips(
    sb,
    camera_ids: list[str],
    model_version: str,
    schema_version: str,
    start: datetime,
    end: datetime,
    limit: int = 200,
) -> list[ClipMeta]:
    """allowlist 카메라의 [start,end) 미처리 clip 을 started_at 순으로 반환."""
    if not camera_ids:
        return []

    rows = (
        sb.table("motion_clips")
        .select(", ".join(_CLIP_FIELDS))
        .in_("camera_id", camera_ids)
        .gte("started_at", start.isoformat())
        .lt("started_at", end.isoformat())
        .order("started_at")
        .limit(limit)
        .execute()
        .data
    )
    clips = [ClipMeta(**{k: r[k] for k in _CLIP_FIELDS}) for r in rows]
    if not clips:
        return []

    ids = [c.id for c in clips]
    done = (
        sb.table("clip_prelabels")
        .select("clip_id")
        .in_("clip_id", ids)
        .eq("model_version", model_version)
        .eq("schema_version", schema_version)
        .execute()
        .data
    )
    done_ids = {r["clip_id"] for r in done}
    return [c for c in clips if c.id not in done_ids]
