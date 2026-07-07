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


_FEEDING = {"eating_paste", "eating_prey", "hand_feeding"}


def summarize_behaviors(labeled) -> dict:
    """샘플 clip 라벨(classify_clip 결과)들 → 행동 관찰 집계. 뼈대(activity)와 별개.

    labeled = [{action, ...}]. motion_score 상위 N개만 claude 태깅한 샘플 → "무슨 행동이
    관찰됐나"(탈피·음수·급여) 신호. 샘플이라 '관찰됨' 수준 — 부재 증명은 아님(못 본 것뿐).
    """
    actions = Counter(it["action"] for it in labeled)
    # claude 인프라 실패(호출/인증/한도) = classify 의 action=error. unseen(파싱실패/게코 부재)은
    # 분석엔 도달한 것이라 제외 — "조용한 한도실패"만 경보 대상(2026-07-07 며칠 샌 원인).
    failed_infra = actions.get("error", 0)
    unseen = actions.get("unseen", 0)  # 게코 안 보임/불명 = '이 clip 은 분석 불필요' 신호(gate v3 전 대체 표시)
    return {
        "sampled_count": len(labeled),
        "actions": dict(actions),
        "failed_infra": failed_infra,
        "unseen": unseen,
        "analyzed_ok": len(labeled) - failed_infra,
        "shed_observed": actions.get("shedding", 0) > 0,
        "drink_observed": actions.get("drinking", 0) > 0,
        "feeding_observed": any(actions.get(a, 0) > 0 for a in _FEEDING),
    }
