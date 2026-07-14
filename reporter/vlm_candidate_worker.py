import fcntl,hashlib,socket,sys,tempfile
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from anthropic import Anthropic,APIConnectionError,InternalServerError,RateLimitError
from supabase import create_client
from reporter import config,r2
from reporter.anthropic_analyzer import SYSTEM_PROMPT,analyze_clip
from reporter.timewin import trigger_window
from reporter.vlm_budget import fair_job_order
from reporter.vlm_candidate_indexer import load_recent_history,load_window_candidates,partition_eligibility
from reporter.vlm_episode import reduce_episodes
from reporter.vlm_frames import extract_six
from reporter.vlm_selector import select_candidates
from reporter.vlm_store import create_run_and_jobs,load_due_jobs,mark_submitted,update_job

LOCK="/tmp/petcam-vlm-candidate-worker.lock"
def _lock():
    f=open(LOCK,"w")
    try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);return f
    except BlockingIOError:f.close();return None
def _month_start(now):
    k=now.astimezone(ZoneInfo("Asia/Seoul"));return k.replace(day=1,hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
def _ledger(sb,now):
    rows=sb.table("clip_vlm_jobs").select("status,cost_usd,reserved_cost_usd").gte("created_at",_month_start(now).isoformat()).execute().data
    actual=sum(float(r.get("cost_usd") or 0) for r in rows);reserved=sum(float(r.get("reserved_cost_usd") or 0) for r in rows if r.get("status") in {"submitted","failed_retryable"});return actual,reserved
def _jobs(selected,run_id):
    ph=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest();out=[]
    for s in selected:out.append({"clip_id":s.clip.id,"slot":s.slot.value,"episode_key":s.episode_key,"rank_features":s.rank_features,"selection_reason":s.selection_reason,"activity_assessment_id":s.clip.assessment_id or "","prelabel_id":s.clip.prelabel_id or "","model_requested":config.VLM_MODEL,"prompt_version":config.VLM_PROMPT_VERSION,"prompt_sha256":ph,"sampler_version":config.VLM_SAMPLER_VERSION,"reserved_cost_usd":str(config.VLM_RESERVED_COST_USD),"pricing_version":"anthropic-sonnet5-intro-through-2026-08-31"})
    return out
def process_jobs(sb,jobs,client):
    stats=defaultdict(int);streak=0
    with tempfile.TemporaryDirectory() as tmp:
        for job in fair_job_order(jobs):
            if streak>=3:break
            try:
                if not mark_submitted(sb,job,_month_start(datetime.now(timezone.utc)).isoformat(),config.VLM_MONTHLY_BUDGET_USD):stats["held"]+=1;continue
                clip=sb.table("motion_clips").select("id,camera_id,started_at,duration_sec,r2_key,motion_score,width,height").eq("id",job["clip_id"]).execute().data[0]
                from reporter.vlm_models import CandidateClip
                c=CandidateClip(clip["id"],clip["camera_id"],datetime.fromisoformat(clip["started_at"].replace("Z","+00:00")),float(clip["duration_sec"]),clip["r2_key"],float(clip.get("motion_score") or 0),clip.get("width"),clip.get("height"))
                mp4=r2.download_clip(c.r2_key,Path(tmp)/f"{c.id}.mp4");paths=extract_six(mp4,Path(tmp)/c.id);res=analyze_clip(client,paths,c,config.VLM_MODEL)
                vals={"status":"held_model_mismatch" if res.model_mismatch else "succeeded","model_actual":res.model_actual,"provider_request_id":res.provider_request_id,"result":res.result,"frames_sampled":6,"input_tokens":res.usage.input_tokens,"cache_creation_input_tokens":res.usage.cache_creation_input_tokens,"cache_read_input_tokens":res.usage.cache_read_input_tokens,"output_tokens":res.usage.output_tokens,"cost_usd":str(res.cost_usd),"completed_at":datetime.now(timezone.utc).isoformat()};update_job(sb,job["id"],vals);stats[vals["status"]]+=1;streak=0
                if res.model_mismatch:break
            except (APIConnectionError,InternalServerError,RateLimitError) as e:
                streak+=1;status="failed_retryable" if job.get("attempt_count",0)<2 else "failed_terminal";update_job(sb,job["id"],{"status":status,"error_code":type(e).__name__});stats[status]+=1
            except Exception as e:
                update_job(sb,job["id"],{"status":"failed_terminal","error_code":type(e).__name__});stats["failed_terminal"]+=1;print(f"[vlm-router] {job['clip_id'][:8]} {type(e).__name__}",file=sys.stderr)
    return dict(stats)
def run(*,sb=None,now=None,enabled=None,client=None):
    enabled=config.VLM_ROUTER_ENABLED if enabled is None else enabled
    if not enabled:print("[vlm-router] disabled — skip");return 0
    now=now or datetime.now(ZoneInfo("Asia/Seoul"));start,end=trigger_window(now);lock=_lock()
    if lock is None:return 0
    try:
        sb=sb or create_client(config.SUPABASE_URL,config.SUPABASE_KEY);actual,reserved=_ledger(sb,now);clips=load_window_candidates(sb,start,end,config.VLM_ACTIVITY_POLICY_VERSION,config.VLM_SELECTOR_VERSION);groups=defaultdict(list)
        for c in clips:groups[c.camera_id].append(c)
        for cam in sorted(groups):
            eligible,reasons=partition_eligibility(groups[cam]);reps=reduce_episodes(eligible,start);history=load_recent_history(sb,cam,now-timedelta(days=7));selected=select_candidates(reps,history,start)[:4]
            runrow={"camera_id":cam,"window_start":start.isoformat(),"window_end":end.isoformat(),"selector_version":config.VLM_SELECTOR_VERSION,"clips_seen":len(groups[cam]),"hard_invalid_count":reasons.get("invalid_input",0),"already_processed_count":sum(v for k,v in reasons.items() if k!="invalid_input"),"episode_count":len(reps),"pool_counts":{},"selected_clip_ids":[s.clip.id for s in selected],"unselected_reason_counts":reasons,"monthly_budget_usd":str(config.VLM_MONTHLY_BUDGET_USD),"month_reserved_usd":str(reserved),"month_actual_usd":str(actual),"producer_host":socket.gethostname(),"producer_run_id":f"{now:%Y%m%dT%H%M%S}"};create_run_and_jobs(sb,runrow,_jobs(selected,runrow["producer_run_id"]))
        stats=process_jobs(sb,load_due_jobs(sb,64),client or Anthropic());print(f"[vlm-router] window={start.isoformat()}..{end.isoformat()} clips={len(clips)} stats={stats}");return 0
    finally:fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
if __name__=="__main__":raise SystemExit(run())
