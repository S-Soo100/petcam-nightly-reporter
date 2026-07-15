from __future__ import annotations

import json
import socket
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

from supabase import create_client

from reporter import config
from reporter.activity_worker import acquire_activity_lock, release_activity_lock
from reporter.vlm_backfill_gate import GateEnrichment, enrich_prepool
from reporter.vlm_backfill_selector import (
    BACKFILL_SELECTOR_VERSION,
    BucketPlan,
    bucket_plans,
    build_prepool,
    select_bucket_candidates,
    source_nights,
)
from reporter.vlm_candidate_indexer import load_recent_history, load_window_candidates, partition_eligibility
from reporter.vlm_candidate_worker import (
    acquire_vlm_lock,
    build_job_rows,
    process_cli_jobs,
    release_vlm_lock,
)
from reporter.vlm_models import SelectedCandidate
from reporter.vlm_store import create_run_and_jobs, load_due_jobs_for_selector

COMPLETE_STATUSES = {"succeeded", "failed_terminal"}
BLOCKING_CODES = {"not_logged_in", "quota_exceeded", "clip_set_mismatch"}


class InsufficientCandidates(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BucketSelection:
    plan: BucketPlan
    clips_seen: int
    prepool_count: int
    selected: tuple[SelectedCandidate, ...]


@dataclass(frozen=True, slots=True)
class WavePlan:
    source_date: date
    camera_id: str
    buckets: tuple[BucketSelection, ...]
    gate_stats: dict[str, int]

    @property
    def selected(self) -> tuple[SelectedCandidate, ...]:
        return tuple(item for bucket in self.buckets for item in bucket.selected)

    @property
    def start(self) -> datetime:
        return self.buckets[0].plan.start

    @property
    def end(self) -> datetime:
        return self.buckets[-1].plan.end

    def to_dict(self) -> dict[str, object]:
        return {
            "source_date": self.source_date.isoformat(),
            "camera": self.camera_id[:8],
            "selected": [
                {"clip": item.clip.id[:8], "slot": item.slot.value, "bucket": bucket.plan.bucket_index}
                for bucket in self.buckets for item in bucket.selected
            ],
            "gate_stats": self.gate_stats,
        }


def _range_rows(sb, table, columns, start, end, *, page_size=1000):
    rows=[];offset=0
    while True:
        page=(sb.table(table).select(columns).gte("started_at",start.isoformat()).lt("started_at",end.isoformat())
              .order("started_at").range(offset,offset+page_size-1).execute().data)
        rows+=page
        if len(page)<page_size:return rows
        offset+=page_size


def choose_target_camera(sb, source_dates: tuple[date, ...]) -> str:
    plans_start=bucket_plans(source_dates[0])[0].start
    plans_end=bucket_plans(source_dates[-1])[-1].end
    rows=_range_rows(sb,"motion_clips","id,camera_id,started_at,duration_sec,r2_key",plans_start,plans_end)
    counts=Counter(row["camera_id"] for row in rows if row.get("r2_key") and float(row.get("duration_sec") or 0)>0)
    if not counts:raise InsufficientCandidates("no valid clips in backfill range")
    return min(counts,key=lambda camera_id:(-counts[camera_id],camera_id))


def _jobs_in_night(sb, source_date: date, camera_id: str):
    plans=bucket_plans(source_date);start=plans[0].start;end=plans[-1].end
    return (sb.table("clip_vlm_jobs").select("*").eq("selector_version",BACKFILL_SELECTOR_VERSION)
            .eq("camera_id",camera_id).gte("window_start",start.isoformat()).lt("window_start",end.isoformat())
            .execute().data)


def next_source_date(sb, camera_id: str, now: datetime) -> date | None:
    last_completed=None
    for source_date in source_nights():
        jobs=_jobs_in_night(sb,source_date,camera_id)
        complete=sum(job.get("status") in COMPLETE_STATUSES for job in jobs)
        if len(jobs)<30 or complete<30:
            if jobs:return source_date
            if last_completed and now < last_completed+timedelta(minutes=55):return None
            return source_date
        completed=[datetime.fromisoformat(job["completed_at"].replace("Z","+00:00")) for job in jobs if job.get("completed_at")]
        if completed:last_completed=max(completed)
    return None


def blocking_error_for_backfill(sb, camera_id: str) -> str | None:
    rows=(sb.table("clip_vlm_jobs").select("status,error_code").eq("selector_version",BACKFILL_SELECTOR_VERSION)
          .eq("camera_id",camera_id).execute().data)
    for row in rows:
        if row.get("status")=="held_model_mismatch":return "model_mismatch"
        if row.get("error_code") in BLOCKING_CODES:return row["error_code"]
    return None


def _attach_snapshot(item: SelectedCandidate, snapshot: dict[str, object]) -> SelectedCandidate:
    return replace(item,rank_features={**item.rank_features,"gate_snapshot":snapshot})


def prepare_wave(
    sb,
    source_date: date,
    camera_id: str,
    *,
    persist: bool=True,
    load_fn=load_window_candidates,
    enrich_fn=enrich_prepool,
    select_fn=select_bucket_candidates,
    history_fn=load_recent_history,
    create_fn=create_run_and_jobs,
) -> WavePlan:
    plans=bucket_plans(source_date);bucket_inputs={};clips_seen={};all_prepool=[]
    for plan in plans:
        loaded=[clip for clip in load_fn(sb,plan.start,plan.end,config.VLM_ACTIVITY_POLICY_VERSION,BACKFILL_SELECTOR_VERSION) if clip.camera_id==camera_id]
        eligible,_reasons=partition_eligibility(loaded);prepool=build_prepool(eligible)
        bucket_inputs[plan.bucket_index]=prepool;clips_seen[plan.bucket_index]=len(loaded);all_prepool+=prepool
    unique={clip.id:clip for clip in all_prepool}
    gate:GateEnrichment=enrich_fn(list(unique.values()),checkpoint=config.GATE_CHECKPOINT_PATH)
    enriched={clip.id:clip for clip in gate.clips}
    history=history_fn(sb,camera_id,plans[0].start-timedelta(days=7))
    buckets=[]
    for plan in plans:
        candidates=[enriched[clip.id] for clip in bucket_inputs[plan.bucket_index] if clip.id in enriched]
        selected=select_fn(candidates,plan,history)
        selected=[_attach_snapshot(item,gate.snapshots.get(item.clip.id,{"source":"missing"})) for item in selected]
        buckets.append(BucketSelection(plan,clips_seen[plan.bucket_index],len(bucket_inputs[plan.bucket_index]),tuple(selected)))
    wave=WavePlan(source_date,camera_id,tuple(buckets),gate.stats)
    selected=wave.selected
    if len(selected)!=30 or len({item.clip.id for item in selected})!=30:
        raise InsufficientCandidates(f"expected 30 unique candidates, got {len(selected)}")
    if not persist:return wave
    for bucket in wave.buckets:
        producer=f"backfill-{source_date.isoformat()}-{bucket.plan.bucket_index}"
        runrow={
            "camera_id":camera_id,"window_start":bucket.plan.start.isoformat(),"window_end":bucket.plan.end.isoformat(),
            "selector_version":BACKFILL_SELECTOR_VERSION,"clips_seen":bucket.clips_seen,"hard_invalid_count":0,
            "already_processed_count":0,"episode_count":bucket.prepool_count,
            "pool_counts":{"prepool":bucket.prepool_count,"selected":len(bucket.selected),"gate":gate.stats},
            "selected_clip_ids":[item.clip.id for item in bucket.selected],"unselected_reason_counts":{},
            "monthly_budget_usd":str(config.VLM_MONTHLY_BUDGET_USD),"month_reserved_usd":"0","month_actual_usd":"0",
            "producer_host":socket.gethostname(),"producer_run_id":producer,
        }
        create_fn(sb,runrow,build_job_rows(bucket.selected,producer,provider="claude_cli_batch"))
    return wave


def run(
    *,sb=None,now=None,dry_run=False,process_fn=process_cli_jobs,prepare_fn=prepare_wave,
    acquire_vlm_lock_fn=acquire_vlm_lock,release_vlm_lock_fn=release_vlm_lock,
    acquire_activity_lock_fn=acquire_activity_lock,release_activity_lock_fn=release_activity_lock,
) -> int:
    now=now or datetime.now(timezone.utc);vlm_lock=acquire_vlm_lock_fn()
    if vlm_lock is None:return 0
    try:
        sb=sb or create_client(config.SUPABASE_URL,config.SUPABASE_KEY)
        regular=load_due_jobs_for_selector(sb,config.VLM_SELECTOR_VERSION,None,None)
        if regular:process_fn(sb,regular);return 0
        camera_id=choose_target_camera(sb,source_nights())
        blocked=blocking_error_for_backfill(sb,camera_id)
        if blocked:print(f"[vlm-backfill] blocked code={blocked}");return 0
        source_date=next_source_date(sb,camera_id,now)
        if source_date is None:print("[vlm-backfill] complete-or-cooldown — no-op");return 0
        activity_lock=acquire_activity_lock_fn()
        if activity_lock is None:print("[vlm-backfill] activity worker busy — defer");return 0
        try:wave=prepare_fn(sb,source_date,camera_id,persist=not dry_run)
        finally:release_activity_lock_fn(activity_lock)
        if dry_run:print(json.dumps(wave.to_dict(),ensure_ascii=False));return 0
        due=load_due_jobs_for_selector(sb,BACKFILL_SELECTOR_VERSION,wave.start,wave.end)
        stats=process_fn(sb,due)
        print(f"[vlm-backfill] source={source_date} selected={len(wave.selected)} stats={stats}")
        return 0
    finally:release_vlm_lock_fn(vlm_lock)


if __name__=="__main__":raise SystemExit(run())
