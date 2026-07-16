from __future__ import annotations

import json
import socket
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from supabase import create_client

from reporter import config, slack
from reporter.vlm_backfill_summary import aggregate_backfill_progress, send_backfill_progress
from reporter.activity_worker import acquire_activity_lock, release_activity_lock
from reporter.vlm_backfill_gate import GateEnrichment, enrich_prepool
from reporter.vlm_backfill_selector import (
    BACKFILL_SELECTOR_VERSION,
    EPOCH_START,
    BucketPlan,
    bucket_plans,
    build_prepool,
    rolling_source_nights,
    select_bucket_candidates,
    source_nights,
)
from reporter.vlm_rolling import remaining_daily_budget, rolling_backfill_allowed_now
from reporter.vlm_store import (
    claim_backfill_source_date,
    load_backfill_ledger,
    load_dedup_clip_ids,
    upsert_backfill_ledger,
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
_KST = ZoneInfo("Asia/Seoul")


def backfill_allowed_now(now: datetime) -> bool:
    """정규 야간 schedule(22/00/02/04)·shared Claude lock 과 겹치지 않게 07:00<=KST<20:00 만 허용."""
    return 7 <= now.astimezone(_KST).hour < 20


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
                {
                    "clip": item.clip.id[:8],
                    "slot": item.slot.value,
                    "bucket": bucket.plan.bucket_index,
                    "gate_source": item.rank_features["gate_snapshot"].get("source", "missing"),
                }
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


def _attach_snapshot(item: SelectedCandidate, snapshot: dict[str, object], plan: BucketPlan) -> SelectedCandidate:
    return replace(item,rank_features={
        **item.rank_features,
        "source_date": plan.source_date.isoformat(),
        "bucket_index": plan.bucket_index,
        "backfill_version": BACKFILL_SELECTOR_VERSION,
        "gate_snapshot": snapshot,
    })


def prepare_wave(
    sb,
    source_date: date,
    camera_id: str,
    *,
    persist: bool=True,
    exclude_clip_ids=frozenset(),
    max_new: int|None=None,
    load_fn=load_window_candidates,
    enrich_fn=enrich_prepool,
    select_fn=select_bucket_candidates,
    history_fn=load_recent_history,
    create_fn=create_run_and_jobs,
) -> WavePlan:
    plans=bucket_plans(source_date);bucket_inputs={};clips_seen={};all_prepool=[]
    for plan in plans:
        # cross-selector 중복 방지: 이미 어떤 clip_vlm_jobs 든 존재하는 clip 은 후보에서 제외.
        loaded=[clip for clip in load_fn(sb,plan.start,plan.end,config.VLM_ACTIVITY_POLICY_VERSION,BACKFILL_SELECTOR_VERSION)
                if clip.camera_id==camera_id and clip.id not in exclude_clip_ids]
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
        selected=[_attach_snapshot(item,gate.snapshots.get(item.clip.id,{"source":"missing"}),plan) for item in selected]
        buckets.append(BucketSelection(plan,clips_seen[plan.bucket_index],len(bucket_inputs[plan.bucket_index]),tuple(selected)))
    # 일일 상한 clamp: 이번 wave 가 새로 만들 job 을 max_new 개로 제한(정규 30/cycle·600/day 준수).
    if max_new is not None:
        remaining=max(0,max_new);capped=[]
        for bucket in buckets:
            take=bucket.selected[:remaining];remaining-=len(take)
            capped.append(BucketSelection(bucket.plan,bucket.clips_seen,bucket.prepool_count,tuple(take)))
        buckets=tuple(capped)
    else:
        buckets=tuple(buckets)
    wave=WavePlan(source_date,camera_id,buckets,gate.stats)
    # rolling: 30 미만도 존재분만 생성(부족 후보), 0 이면 아무 것도 생성 안 함(worker 가 no_candidates 처리).
    seen_ids=set()
    for item in wave.selected:
        if item.clip.id in seen_ids:raise InsufficientCandidates("duplicate clip within wave")
        seen_ids.add(item.clip.id)
    if not persist or not wave.selected:return wave
    for bucket in wave.buckets:
        if not bucket.selected:continue  # 빈 bucket 은 run 생성 안 함(빈 run 오염 방지)
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


_LEDGER_SKIP = {"no_candidates", "blocked"}
_OPEN_STATUSES = ("queued", "failed_retryable", "submitted", "processing", "held_model_mismatch")


def next_rolling_source_date(closed_nights, ledger_status, job_state):
    """가장 오래된 미처리 closed night → (date, 'new'|'resume'). ledger no_candidates/blocked skip,
    jobs open==0 은 완료로 파생(ledger 기록 없이). backlog 없으면 (None, None)."""
    for d in closed_nights:
        key = d.isoformat()
        if ledger_status.get(key) in _LEDGER_SKIP:
            continue
        js = job_state.get(key)
        if not js or js.get("created", 0) == 0:
            return d, "new"
        if js.get("open", 0) > 0:
            return d, "resume"
    return None, None


def _job_state_by_date(sb):
    rows = (sb.table("clip_vlm_jobs").select("status,rank_features")
            .eq("selector_version", BACKFILL_SELECTOR_VERSION).execute().data)
    out = {}
    for r in rows:
        d = (r.get("rank_features") or {}).get("source_date")
        if not d:
            continue
        b = out.setdefault(d, {"created": 0, "open": 0, "succeeded": 0, "terminal": 0})
        b["created"] += 1
        st = r.get("status")
        if st == "succeeded":
            b["succeeded"] += 1
        elif st == "failed_terminal":
            b["terminal"] += 1
        elif st in _OPEN_STATUSES:
            b["open"] += 1
    return out


def choose_target_camera_for_night(sb, source_date):
    plans = bucket_plans(source_date)
    rows = _range_rows(sb, "motion_clips", "id,camera_id,started_at,duration_sec,r2_key", plans[0].start, plans[-1].end)
    counts = Counter(r["camera_id"] for r in rows if r.get("r2_key") and float(r.get("duration_sec") or 0) > 0)
    if not counts:
        return None
    return min(counts, key=lambda camera_id: (-counts[camera_id], camera_id))


def _night_camera(sb, source_date):
    plans = bucket_plans(source_date)
    rows = (sb.table("clip_vlm_jobs").select("camera_id").eq("selector_version", BACKFILL_SELECTOR_VERSION)
            .gte("window_start", plans[0].start.isoformat()).lt("window_start", plans[-1].end.isoformat())
            .limit(1).execute().data)
    return rows[0]["camera_id"] if rows else None


def _sync_ledger(sb, source_date, scope, status_hint):
    """처리 후 night job 상태로 ledger upsert. 전부 terminal 이면 completed."""
    if scope is None:
        return
    js = _job_state_by_date(sb).get(source_date.isoformat(), {"created": 0, "open": 0, "succeeded": 0, "terminal": 0})
    status = "completed" if js["created"] > 0 and js["open"] == 0 else status_hint
    upsert_backfill_ledger(sb, BACKFILL_SELECTOR_VERSION, source_date, scope, status,
                           target=js["created"], created=js["created"],
                           processed=js["succeeded"] + js["terminal"], succeeded=js["succeeded"], terminal=js["terminal"])


def _report_progress(sb, source_date, now, stats, due, host, ledger_status, closed_nights, job_state, send_fn):
    """실제 job 처리 cycle 만 rolling 진행률 Slack 1회. due 비면 로그만."""
    if not due:
        return
    waiting = _waiting_dates(closed_nights, ledger_status, job_state)
    summary = aggregate_backfill_progress(sb, source_date=source_date, host=host, now=now,
                                          this_run_stats=stats, processed_target=len(due), waiting_dates=waiting)
    if not send_backfill_progress(summary, send_fn=send_fn):
        print(f"[vlm-backfill] slack=FAIL source={source_date}")


def _waiting_dates(closed_nights, ledger_status, job_state):
    """아직 처리되지 않은 closed nights 수(new 또는 open>0)."""
    n = 0
    for d in closed_nights:
        if ledger_status.get(d.isoformat()) in _LEDGER_SKIP:
            continue
        js = job_state.get(d.isoformat())
        if not js or js.get("created", 0) == 0 or js.get("open", 0) > 0:
            n += 1
    return n


def run(
    *, sb=None, now=None, dry_run=False, process_fn=process_cli_jobs, prepare_fn=prepare_wave,
    acquire_vlm_lock_fn=acquire_vlm_lock, release_vlm_lock_fn=release_vlm_lock,
    acquire_activity_lock_fn=acquire_activity_lock, release_activity_lock_fn=release_activity_lock,
    send_fn=None, allowed_fn=rolling_backfill_allowed_now, start_date=EPOCH_START,
) -> int:
    now = now or datetime.now(timezone.utc)
    if send_fn is None:
        send_fn = slack.post_slack
    # schedule guard: 정규 VLM ±30분이면 lock/DB/R2/Gate/Claude 전에 no-op(fail-closed).
    if not allowed_fn(now):
        print("[vlm-backfill] near regular VLM window — no-op")
        return 0
    vlm_lock = acquire_vlm_lock_fn()
    if vlm_lock is None:
        return 0  # 정규 VLM 이 lock 보유 → 조용히 양보
    try:
        sb = sb or create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        host = socket.gethostname()
        closed = rolling_source_nights(start_date, now)
        if not closed:
            print("[vlm-backfill] no closed nights yet — no-op")
            return 0
        ledger = {r["source_date"]: r["status"] for r in load_backfill_ledger(sb, BACKFILL_SELECTOR_VERSION)}
        job_state = _job_state_by_date(sb)
        source_date, mode = next_rolling_source_date(closed, ledger, job_state)
        if source_date is None:
            print("[vlm-backfill] no backlog — no-op")
            return 0
        if mode == "resume":
            plans = bucket_plans(source_date); start = plans[0].start; end = plans[-1].end
            due = load_due_jobs_for_selector(sb, BACKFILL_SELECTOR_VERSION, start, end)
            stats = process_fn(sb, due)
            scope = _night_camera(sb, source_date)
            _sync_ledger(sb, source_date, scope, "processing")
            print(f"[vlm-backfill] resume source={source_date} due={len(due)} stats={stats}")
            _report_progress(sb, source_date, now, stats, due, host, ledger, closed, _job_state_by_date(sb), send_fn)
            return 0
        # mode == "new": 신규 wave. 일일 상한·camera·dedup·부족 후보 처리.
        remaining = remaining_daily_budget(sb, now)
        if remaining <= 0:
            print("[vlm-backfill] daily cap reached — no-op")
            return 0
        camera_id = choose_target_camera_for_night(sb, source_date)
        if camera_id is None:
            upsert_backfill_ledger(sb, BACKFILL_SELECTOR_VERSION, source_date, "__none__", "no_candidates")
            print(f"[vlm-backfill] source={source_date} no motion clips — no_candidates")
            return 0
        blocked = blocking_error_for_backfill(sb, camera_id)
        if blocked:
            upsert_backfill_ledger(sb, BACKFILL_SELECTOR_VERSION, source_date, camera_id, "blocked", last_error=blocked)
            print(f"[vlm-backfill] blocked code={blocked}")
            return 0
        claim_backfill_source_date(sb, BACKFILL_SELECTOR_VERSION, source_date, camera_id)  # 원자 claim(멱등 진행)
        exclude_ids = load_dedup_clip_ids(sb)  # cross-selector 중복 방지
        activity_lock = acquire_activity_lock_fn()
        if activity_lock is None:
            print("[vlm-backfill] activity worker busy — defer")
            return 0
        try:
            wave = prepare_fn(sb, source_date, camera_id, persist=not dry_run,
                              exclude_clip_ids=exclude_ids, max_new=min(30, remaining))
        finally:
            release_activity_lock_fn(activity_lock)
        if dry_run:
            print(json.dumps(wave.to_dict(), ensure_ascii=False))
            return 0
        selected_n = len(wave.selected)
        if selected_n == 0:
            upsert_backfill_ledger(sb, BACKFILL_SELECTOR_VERSION, source_date, camera_id, "no_candidates")
            print(f"[vlm-backfill] source={source_date} no_candidates")
            return 0
        due = load_due_jobs_for_selector(sb, BACKFILL_SELECTOR_VERSION, wave.start, wave.end)
        stats = process_fn(sb, due)
        _sync_ledger(sb, source_date, camera_id, "insufficient_candidates" if selected_n < 30 else "processing")
        print(f"[vlm-backfill] source={source_date} selected={selected_n} stats={stats}")
        _report_progress(sb, source_date, now, stats, due, host, ledger, closed, _job_state_by_date(sb), send_fn)
        return 0
    finally:
        release_vlm_lock_fn(vlm_lock)


if __name__ == "__main__":
    raise SystemExit(run())
