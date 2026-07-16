"""미처리 motion_clips 선택 — allowlist 카메라 + 완전한 current-policy evidence 가 없는 것.

motion_clips 는 불변(상태 컬럼 금지, 지시문 §438). "처리됨" 의 정의를 self-healing 을 위해
강화한다(설계 §5): current policy assessment 가 존재하는 것만으로는 부족하고, 그 assessment 가
가리키는 prelabel 이 실제로 존재하며 `frames_sampled >= min_frames` 여야 완료로 인정한다.
따라서 불완전(0~5프레임) evidence 로 굳은 clip 은 다음 cycle 에 다시 선정돼 정상 재처리된다.
같은 완전 버전 재실행은 자연히 0건(멱등), Gate 버전이 바뀌면 전체가 다시 미처리로 잡힌다.
"""

from __future__ import annotations

from datetime import datetime

from reporter.indexer import ClipMeta

_CLIP_FIELDS = ("id", "camera_id", "started_at", "duration_sec", "r2_key", "motion_score")
_PRE_BATCH = 200  # prelabel id in_ 조회 배치 크기 (audit 와 동일 관례)


def list_unprocessed_clips(
    sb,
    camera_ids: list[str],
    policy_version: str,
    start: datetime,
    end: datetime,
    *,
    min_frames: int = 6,
    limit: int = 200,
    page_size: int = 500,
) -> list[ClipMeta]:
    """allowlist 카메라의 [start,end) 미처리 clip 을 started_at 순으로 최대 limit 개 반환.

    미처리 = 아래 중 하나인 clip (완료 조건의 부정):
    - current policy assessment 가 **없음** (prelabel 만 있고 assessment 실패 = 하드닝 4)
    - assessment 가 있으나 참조 prelabel 이 없음 (FK 대상 소실)
    - 참조 prelabel 의 `frames_sampled < min_frames` (불완전 evidence self-heal = 설계 §5)
    policy_version 이 바뀌면 그 clip 은 current assessment 가 없어 자동 재평가(하드닝 3).

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
        done_ids = _done_clip_ids(sb, ids, policy_version, min_frames)
        out.extend(c for c in clips if c.id not in done_ids)
        if len(rows) < page_size:
            break  # 마지막 페이지
        offset += page_size
    return out[:limit]


def _done_clip_ids(sb, clip_ids: list[str], policy_version: str, min_frames: int) -> set[str]:
    """이 페이지에서 '완료'로 인정할 clip_id 집합 (2단계 검증).

    1) current policy assessment 를 (clip_id, prelabel_id) 로 로드
    2) 참조 prelabel 을 (id, frames_sampled) 로 배치 로드
    3) prelabel 이 존재하고 frames_sampled >= min_frames 인 assessment 의 clip 만 완료.
    """
    assess = (
        sb.table("clip_activity_assessments")
        .select("clip_id, prelabel_id")
        .in_("clip_id", clip_ids)
        .eq("policy_version", policy_version)
        .execute()
        .data
    )
    pre_ids = sorted({a["prelabel_id"] for a in assess if a.get("prelabel_id")})
    frames_by_id: dict[str, int | None] = {}
    for i in range(0, len(pre_ids), _PRE_BATCH):
        chunk = pre_ids[i : i + _PRE_BATCH]
        pls = (
            sb.table("clip_prelabels")
            .select("id, frames_sampled")
            .in_("id", chunk)
            .execute()
            .data
        )
        for p in pls:
            frames_by_id[p["id"]] = p.get("frames_sampled")

    done: set[str] = set()
    for a in assess:
        pid = a.get("prelabel_id")
        if not pid:
            continue  # assessment 는 있으나 prelabel 링크 없음 → 불완전
        fs = frames_by_id.get(pid)
        if fs is not None and fs >= min_frames:
            done.add(a["clip_id"])
    return done
