"""camera_clips를 UUID keyset cursor로 훑는 labeling triage 후보 indexer."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from reporter.labeling_triage_models import LabelingTriageClip

_CLIP_FIELDS = "id,camera_id,started_at,duration_sec,r2_key,has_motion"


def list_labeling_triage_candidates(
    sb,
    *,
    start: datetime,
    end: datetime,
    limit: int,
    page_size: int = 500,
    identity_for_clip: Callable[[str], str],
) -> list[LabelingTriageClip]:
    """session/owner 결정/동일 identity를 제외하며 페이지 끝까지 계속 찾는다."""
    if limit <= 0 or page_size <= 0:
        return []
    out: list[LabelingTriageClip] = []
    cursor: str | None = None
    while len(out) < limit:
        query = (
            sb.table("camera_clips")
            .select(_CLIP_FIELDS)
            .eq("has_motion", True)
            .gte("started_at", start.isoformat())
            .lt("started_at", end.isoformat())
            .order("id")
            .limit(page_size)
        )
        if cursor is not None:
            query = query.gt("id", cursor)
        rows = query.execute().data
        if not rows:
            break
        cursor = rows[-1]["id"]
        eligible_rows = [r for r in rows if r.get("has_motion") is True and r.get("r2_key")]
        ids = [r["id"] for r in eligible_rows]
        if ids:
            sessions = (
                sb.table("clip_labeling_sessions")
                .select("clip_id")
                .in_("clip_id", ids)
                .execute()
                .data
            )
            triage_rows = (
                sb.table("clip_labeling_triage")
                .select("clip_id,owner_decision,evidence_snapshot")
                .in_("clip_id", ids)
                .execute()
                .data
            )
            session_ids = {r["clip_id"] for r in sessions}
            triage_by_id = {r["clip_id"]: r for r in triage_rows}
            for row in eligible_rows:
                clip_id = row["id"]
                if clip_id in session_ids:
                    continue
                triage = triage_by_id.get(clip_id)
                if triage is not None:
                    if triage.get("owner_decision") in {"label", "skip"}:
                        continue
                    evidence = triage.get("evidence_snapshot") or {}
                    if evidence.get("identity") == identity_for_clip(clip_id):
                        continue
                out.append(LabelingTriageClip(
                    id=clip_id,
                    camera_id=row["camera_id"],
                    started_at=row["started_at"],
                    duration_sec=float(row.get("duration_sec") or 0.0),
                    r2_key=row["r2_key"],
                ))
                if len(out) >= limit:
                    break
        if len(rows) < page_size:
            break
    return out
