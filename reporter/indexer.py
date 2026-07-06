"""terra motion_clips 에서 윈도우 clip 조회 (B방식 — started_at 시간 인덱스).

object store(R2)는 시간범위 조회가 약하고, camera_clips 는 레거시(06-17 이후 0).
motion_clips 가 "R2 객체들의 시간 인덱스" 역할 (architecture §10.1).
"""
from dataclasses import dataclass
from datetime import datetime

from supabase import create_client

from reporter import config


@dataclass(frozen=True, slots=True)
class ClipMeta:
    id: str
    camera_id: str
    started_at: str
    duration_sec: float
    r2_key: str
    motion_score: float


def list_clips_for_window(start: datetime, end: datetime) -> list[ClipMeta]:
    """[start, end) 윈도우의 clip 조회. started_at = 녹화시각(SNTP UTC)."""
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    rows = (
        sb.table("motion_clips")
        .select("id, camera_id, started_at, duration_sec, r2_key, motion_score")
        .gte("started_at", start.isoformat())
        .lt("started_at", end.isoformat())
        .order("started_at")
        .execute()
        .data
    )
    # r2_key IS NOT NULL 필터 불필요 — terra DB-last 라 row=영상 존재 (architecture §10.1)
    return [ClipMeta(**r) for r in rows]
