"""윈도우 clip → 활동 요약. W4a: 활동량·시간대(리포트 뼈대)는 DB 필드만으로(claude 0회).

motion_clips 자체가 모션 트리거 → clip 존재 = 그 시각 활동. 그래서 활동량(clip 수·duration 합)과
활동시간대(started_at 분포)는 claude 없이 산출한다. 행동 종류 태깅(탈피·음수 등)은 비싸고
샘플로 충분하므로 W4b 에서 별도(summarize_behaviors) — 여기선 뼈대만.
"""
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def summarize_activity(clips) -> dict:
    """ClipMeta 리스트 → 활동량/시간대. started_at(UTC) 을 KST 시(hour)로 분포화.

    clips = indexer.list_clips_for_window 결과(list[ClipMeta]). 라벨 불필요 — clip=활동.
    """
    total_sec = sum(c.duration_sec for c in clips)
    hours = Counter(
        datetime.fromisoformat(c.started_at).astimezone(_KST).hour
        for c in clips
    )
    return {
        "clip_count": len(clips),
        "active_minutes": round(total_sec / 60, 1),
        "peak_hour_kst": hours.most_common(1)[0][0] if hours else None,
        "hourly_kst": dict(hours),
    }
