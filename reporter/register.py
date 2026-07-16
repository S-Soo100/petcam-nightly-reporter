"""하이라이트 clip → camera_clips 미러 + behavior_logs(vlm 후보) 등록.

왜 미러가 필요한가: behavior_logs.clip_id FK → camera_clips(id). 앱 추론뷰·라벨링 큐도
camera_clips 만 조회. 운영 motion_clips 만 있으면 앱에 안 뜸 → motion_clips.id 를 재사용해
camera_clips 로 미러(추적성 + FK 충족)한 뒤 behavior_logs 기록.
(recipe: petcam-lab scripts/register_motion_candidates.py 의 live 단건판)

방법론(cherry-pick 방지): claude 라벨은 GT 아님 → source='vlm', verified=False 로만 저장.
사람 육안 확정은 라벨링 웹에서(→ behavior_labels). 평가셋 manifest 는 안 건드림.
멱등: camera_clips id 존재 시 skip. behavior_logs UNIQUE(clip_id, source) 가 2차 방어.
"""
from reporter import config
from reporter.indexer import ClipMeta


def should_register(action: str) -> bool:
    """등록 대상 판정 — moving(흔함)/basking(휴식·비정보성)/error(분류실패)/unseen(게코부재)/shedding은 제외."""
    return bool(action) and action not in config.REGISTER_SKIP_ACTIONS


def register_highlight(
    sb, clip: ClipMeta, action: str, confidence: float | None, reasoning: str = ""
) -> str:
    """clip 1건 등록. 반환 'registered' | 'exists'. DB 에러는 raise(호출자가 격리).

    sb: service_role supabase client — RLS 우회로 camera_clips/behavior_logs 쓰기.
    """
    existing = sb.table("camera_clips").select("id").eq("id", clip.id).execute().data
    if existing:
        return "exists"  # 이미 미러됨(멱등) — behavior_logs 도 있을 것

    sb.table("camera_clips").insert(
        {
            "id": clip.id,  # motion_clips.id 재사용 (추적성 + behavior_logs FK 충족)
            "user_id": config.REGISTER_OWNER_USER_ID,
            "pet_id": config.REGISTER_PET_ID,
            "camera_id": clip.camera_id,
            "source": "camera",  # camera_clips_source_check: camera/upload/youtube 만
            "started_at": clip.started_at,
            "duration_sec": clip.duration_sec,
            "has_motion": True,
            "file_path": clip.r2_key,  # NOT NULL — 로컬 원본 없어 r2_key 로 식별
            "r2_key": clip.r2_key,
        }
    ).execute()
    sb.table("behavior_logs").insert(
        {
            "clip_id": clip.id,
            "frame_idx": 0,  # 클립 단위 단일 라벨
            "action": action,
            "confidence": confidence,
            "source": "vlm",
            "vlm_model": config.REGISTER_VLM_MODEL,
            "reasoning": reasoning or "",
            "verified": False,
            "notes": "nightly-board auto (claude 후보, 육안 미확정)",
        }
    ).execute()
    return "registered"
