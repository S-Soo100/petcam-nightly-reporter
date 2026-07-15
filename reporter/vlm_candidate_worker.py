import fcntl,hashlib,socket,sys,tempfile
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from anthropic import (Anthropic,APIConnectionError,AuthenticationError,BadRequestError,
                       InternalServerError,PermissionDeniedError,RateLimitError)
from botocore.exceptions import BotoCoreError,ClientError
from supabase import create_client
from reporter import config,r2
from reporter.anthropic_analyzer import SYSTEM_PROMPT,analyze_clip
from reporter.claude_cli_analyzer import CliBatchError,analyze_batch,check_cli_auth
from reporter.timewin import trigger_window
from reporter.vlm_budget import fair_job_order
from reporter.vlm_candidate_indexer import load_recent_history,load_window_candidates,partition_eligibility
from reporter.vlm_episode import reduce_episodes
from reporter.vlm_frames import FrameExtractionError,extract_six
from reporter.vlm_selector import select_candidates
from reporter.vlm_store import create_run_and_jobs,load_due_jobs,mark_submitted,update_job

LOCK="/tmp/petcam-vlm-candidate-worker.lock"


def cap_night_selections(selected_by_camera, camera_counts, global_remaining):
    """슬롯 우선 round-robin으로 global/camera 야간 상한을 함께 강제한다."""
    out={camera_id:[] for camera_id in selected_by_camera}
    remaining_by_camera={camera_id:max(0,config.VLM_MAX_PER_CAMERA_NIGHT-camera_counts.get(camera_id,0)) for camera_id in selected_by_camera}
    for slot in ("customer_highlight","subtle_behavior","diversity_discovery","exclusion_audit"):
        for camera_id in sorted(selected_by_camera):
            if global_remaining<=0:return out
            if remaining_by_camera[camera_id]<=0:continue
            item=next((x for x in selected_by_camera[camera_id] if x.slot.value==slot),None)
            if item is None:continue
            out[camera_id].append(item);remaining_by_camera[camera_id]-=1;global_remaining-=1
    return out


def failure_status(job):
    """RPC가 현재 attempt를 +1 하기 전 읽은 row 기준으로 총 2회만 허용한다."""
    return "failed_retryable" if int(job.get("attempt_count") or 0)==0 else "failed_terminal"


def _night_bounds(window_end):
    local=window_end.astimezone(ZoneInfo("Asia/Seoul"))
    day=local.date() if local.hour>=20 else (local-timedelta(days=1)).date()
    start=datetime.combine(day,datetime.min.time(),ZoneInfo("Asia/Seoul")).replace(hour=20)
    return start.astimezone(timezone.utc),(start+timedelta(hours=8)).astimezone(timezone.utc)


def _night_counts(sb,night_start,night_end):
    rows=sb.table("clip_vlm_jobs").select("camera_id").gte("window_start",night_start.isoformat()).lt("window_start",night_end.isoformat()).execute().data
    counts=defaultdict(int)
    for row in rows:counts[row["camera_id"]]+=1
    return len(rows),dict(counts)
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
    cli=config.VLM_PROVIDER=="claude_cli_batch"
    for s in selected:out.append({"clip_id":s.clip.id,"slot":s.slot.value,"episode_key":s.episode_key,"rank_features":s.rank_features,"selection_reason":s.selection_reason,"activity_assessment_id":s.clip.assessment_id or "","prelabel_id":s.clip.prelabel_id or "","model_requested":config.VLM_MODEL,"prompt_version":config.VLM_PROMPT_VERSION,"prompt_sha256":ph,"sampler_version":config.VLM_SAMPLER_VERSION,"reserved_cost_usd":"0" if cli else str(config.VLM_RESERVED_COST_USD),"pricing_version":"claude-code-subscription-v1" if cli else "anthropic-sonnet5-intro-through-2026-08-31"})
    return out


def _split(total,count,index):
    quotient,remainder=divmod(int(total),count)
    return quotient+(1 if index<remainder else 0)


def process_cli_jobs(sb,jobs,*,analyzer=analyze_batch,download_fn=r2.download_clip,extract_fn=extract_six,auth_check=check_cli_auth):
    stats=defaultdict(int)
    if not jobs:return {}
    try:auth_check()
    except CliBatchError as exc:
        for job in jobs:update_job(sb,job["id"],{"error_code":exc.code});stats[exc.code]+=1
        return dict(stats)
    groups=defaultdict(list)
    for job in fair_job_order(jobs):groups[(job["selector_run_id"],job["camera_id"])].append(job)
    with tempfile.TemporaryDirectory() as tmp:
        for batch in groups.values():
            submitted=[];frames={}
            for job in batch[:config.VLM_MAX_PER_CAMERA_WINDOW]:
                try:
                    if not mark_submitted(sb,job,_month_start(datetime.now(timezone.utc)).isoformat(),config.VLM_MONTHLY_BUDGET_USD):
                        stats["held"]+=1;continue
                    submitted.append(job)
                    clip=sb.table("motion_clips").select("id,r2_key").eq("id",job["clip_id"]).execute().data[0]
                    mp4=download_fn(clip["r2_key"],Path(tmp)/f"{job['clip_id']}.mp4")
                    frames[job["clip_id"]]=extract_fn(mp4,Path(tmp)/job["clip_id"])
                except Exception as exc:
                    status=failure_status(job);update_job(sb,job["id"],{"status":status,"error_code":type(exc).__name__});stats[status]+=1
            ready=[job for job in submitted if job["clip_id"] in frames]
            if not ready:continue
            try:
                result=analyzer(frames,config.VLM_MODEL)
            except CliBatchError as exc:
                for job in ready:
                    status=failure_status(job);update_job(sb,job["id"],{"status":status,"error_code":exc.code});stats[status]+=1
                break
            count=len(ready)
            for index,job in enumerate(ready):
                classification=result.results.get(job["clip_id"],{})
                values={
                    "status":"held_model_mismatch" if result.model_mismatch else "succeeded",
                    "model_actual":result.model_actual,
                    "provider_request_id":result.provider_request_id,
                    "result":{**classification,"provider":"claude_cli_batch","provider_estimated_cost_usd":result.provider_estimated_cost_usd/count},
                    "frames_sampled":6,
                    "input_tokens":_split(result.usage.input_tokens,count,index),
                    "cache_creation_input_tokens":_split(result.usage.cache_creation_input_tokens,count,index),
                    "cache_read_input_tokens":_split(result.usage.cache_read_input_tokens,count,index),
                    "output_tokens":_split(result.usage.output_tokens,count,index),
                    "cost_usd":"0",
                    "error_code":None,
                    "completed_at":datetime.now(timezone.utc).isoformat(),
                }
                update_job(sb,job["id"],values);stats[values["status"]]+=1
            if result.model_mismatch:break
    return dict(stats)
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
            except (APIConnectionError,InternalServerError,RateLimitError,BotoCoreError,ClientError,FrameExtractionError) as e:
                streak+=1;status=failure_status(job);update_job(sb,job["id"],{"status":status,"error_code":type(e).__name__});stats[status]+=1
            except (AuthenticationError,PermissionDeniedError,BadRequestError) as e:
                update_job(sb,job["id"],{"status":"failed_terminal","error_code":type(e).__name__});stats["failed_terminal"]+=1;break
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
        contexts={};selected_by_camera={}
        for cam in sorted(groups):
            eligible,reasons=partition_eligibility(groups[cam]);reps=reduce_episodes(eligible,start);history=load_recent_history(sb,cam,now-timedelta(days=7));selected=select_candidates(reps,history,start)[:config.VLM_MAX_PER_CAMERA_WINDOW]
            contexts[cam]=(reasons,reps);selected_by_camera[cam]=selected
        night_start,night_end=_night_bounds(end);night_total,camera_counts=_night_counts(sb,night_start,night_end)
        capped=cap_night_selections(selected_by_camera,camera_counts,max(0,config.VLM_MAX_PER_NIGHT-night_total))
        for cam in sorted(groups):
            reasons,reps=contexts[cam];selected=capped[cam]
            runrow={"camera_id":cam,"window_start":start.isoformat(),"window_end":end.isoformat(),"selector_version":config.VLM_SELECTOR_VERSION,"clips_seen":len(groups[cam]),"hard_invalid_count":reasons.get("invalid_input",0),"already_processed_count":sum(v for k,v in reasons.items() if k!="invalid_input"),"episode_count":len(reps),"pool_counts":{},"selected_clip_ids":[s.clip.id for s in selected],"unselected_reason_counts":reasons,"monthly_budget_usd":str(config.VLM_MONTHLY_BUDGET_USD),"month_reserved_usd":str(reserved),"month_actual_usd":str(actual),"producer_host":socket.gethostname(),"producer_run_id":f"{now:%Y%m%dT%H%M%S}"};create_run_and_jobs(sb,runrow,_jobs(selected,runrow["producer_run_id"]))
        due=load_due_jobs(sb,config.VLM_MAX_PER_NIGHT)
        stats=process_cli_jobs(sb,due) if config.VLM_PROVIDER=="claude_cli_batch" else process_jobs(sb,due,client or Anthropic())
        print(f"[vlm-router] provider={config.VLM_PROVIDER} window={start.isoformat()}..{end.isoformat()} clips={len(clips)} stats={stats}");return 0
    finally:fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
if __name__=="__main__":raise SystemExit(run())
