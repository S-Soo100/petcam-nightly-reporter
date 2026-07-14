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
    policy_version: str,
    start: datetime,
    end: datetime,
    limit: int = 200,
    page_size: int = 500,
) -> list[ClipMeta]:
    """allowlist 카메라의 [start,end) 미처리 clip 을 started_at 순으로 최대 limit 개 반환.

    미처리 = clip_activity_assessments 에 (현재 policy_version) 이 **없는** clip. assessment 기준:
    - prelabel 만 있고 assessment 저장 실패한 clip 도 재처리(self-healing, 영구 pending 방지 = 하드닝 4)
    - policy_version 변경 시 자동 재평가 대상(하드닝 3, worker 가 기존 prelabel 재사용)

    **pagination 필수**: motion_clips 를 limit 로 먼저 자른 뒤 done 을 빼면, 오래된 clip 이 전부
    처리된 순간 그 prefix 만 반복 조회돼 최신 미처리가 영구 굶는다(starvation — 카메라 A 하루
    324 clip vs limit 200 에서 실제 발생). 그래서 페이지 단위로 조회하며 미처리를 limit 개 채운다.
    """
    if not camera_ids:
        return []

    out: list[ClipMeta] = []
    offset = 0
    while len(out) < limit:
        rows = (
            sb.table("motion_clips")
            .select(", ".join(_CLIP_FIELDS))
            .in_("camera_id", camera_ids)
            .gte("started_at", start.isoformat())
            .lt("started_at", end.isoformat())
            .order("started_at")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        if not rows:
            break
        clips = [ClipMeta(**{k: r[k] for k in _CLIP_FIELDS}) for r in rows]
        ids = [c.id for c in clips]
        done = (
            sb.table("clip_activity_assessments")
            .select("clip_id")
            .in_("clip_id", ids)
            .eq("policy_version", policy_version)
            .execute()
            .data
        )
        done_ids = {r["clip_id"] for r in done}
        out.extend(c for c in clips if c.id not in done_ids)
        if len(rows) < page_size:
            break  # 마지막 페이지
        offset += page_size
    return out[:limit]
