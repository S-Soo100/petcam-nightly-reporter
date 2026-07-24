from reporter.short_clip_retention_store import load_system_excluded_clip_ids

def create_run_and_jobs(sb,run,jobs):
    if len(jobs)>4:raise ValueError("max 4 jobs")
    return sb.rpc("fn_create_clip_vlm_selector_run",{"p_run":run,"p_jobs":jobs}).execute().data
def mark_submitted(sb,job,month_start,budget):
    return bool(sb.rpc("fn_reserve_clip_vlm_job",{"p_job_id":job["id"],"p_month_start":month_start,"p_budget_usd":str(budget)}).execute().data)
def update_job(sb,job_id,values):
    rows=sb.table("clip_vlm_jobs").update(values).eq("id",job_id).execute().data
    if len(rows)!=1:raise RuntimeError("job update failed")
    return rows[0]
def claim_vlm_slack_notification(sb,selector_version,start,end,host,run_id):
    """원자 claim: 같은 (selector,window,host) 로 최초 1회만 True. 이후·동시 실행은 False.

    durable(DB) idempotency — 프로세스 메모리 아님. unique constraint + INSERT ON CONFLICT
    DO NOTHING 로 동시 실행도 한 번만 통과한다(§Item2)."""
    return bool(sb.rpc("fn_claim_vlm_slack_notification",{
        "p_selector":selector_version,"p_window_start":start.isoformat(),
        "p_window_end":end.isoformat(),"p_host":host,"p_run_id":run_id,
    }).execute().data)

def release_vlm_slack_notification(sb,selector_version,start,end,host):
    """Slack 전송 실패 시 claim 을 해제해 다음 실행이 재전송할 수 있게 한다(재전송 정책)."""
    sb.rpc("fn_release_vlm_slack_notification",{
        "p_selector":selector_version,"p_window_start":start.isoformat(),
        "p_window_end":end.isoformat(),"p_host":host,
    }).execute()

def paginate(make_query,page_size=1000):
    """PostgREST 1000행 상한을 넘겨 전량 조회. id 오름차순 stable range pagination."""
    rows=[];offset=0
    while True:
        page=make_query().order("id").range(offset,offset+page_size-1).execute().data
        rows+=page
        if len(page)<page_size:return rows
        offset+=page_size

def claim_backfill_source_date(sb,selector_version,source_date,scope):
    """rolling backfill 날짜 원자 claim(동시 worker 중복 wave 방지). 최초 1회만 True."""
    return bool(sb.rpc("fn_claim_backfill_source_date",{
        "p_selector":selector_version,"p_source_date":source_date.isoformat(),"p_scope":scope,
    }).execute().data)

def release_backfill_claim(sb,selector_version,source_date,scope):
    """claim 해제(H1.3). DB 가 해당 selector/source_date 에 job 이 하나도 없을 때만 삭제하도록 강제."""
    return bool(sb.rpc("fn_release_backfill_claim",{
        "p_selector":selector_version,"p_source_date":source_date.isoformat(),"p_scope":scope,
    }).execute().data)

def upsert_backfill_ledger(sb,selector_version,source_date,scope,status,*,target=0,created=0,processed=0,succeeded=0,terminal=0,last_error=None):
    sb.rpc("fn_upsert_backfill_ledger",{
        "p_selector":selector_version,"p_source_date":source_date.isoformat(),"p_scope":scope,
        "p_status":status,"p_target":target,"p_created":created,"p_processed":processed,
        "p_succeeded":succeeded,"p_terminal":terminal,"p_last_error":last_error,
    }).execute()

def load_backfill_ledger(sb,selector_version):
    return paginate(lambda:sb.table("vlm_backfill_ledger").select("id,source_date,scope,status").eq("selector_version",selector_version))

def load_dedup_clip_ids(sb):
    """모든 selector 의 clip_vlm_jobs clip_id 집합(cross-selector 중복 분석 방지). 1000행+ 전량."""
    return {r["clip_id"] for r in paginate(lambda:sb.table("clip_vlm_jobs").select("id,clip_id")) if r.get("clip_id")}

def load_due_jobs(sb,limit=64):
    rows=[]
    for status in ("queued","failed_retryable"):
        rows+=sb.table("clip_vlm_jobs").select("*").eq("status",status).order("queued_at").limit(limit).execute().data
    return rows[:limit]

def load_due_jobs_for_selector(sb,selector_version,start=None,end=None,limit=64):
    rows=[]
    for status in ("queued","failed_retryable"):
        q=sb.table("clip_vlm_jobs").select("*").eq("selector_version",selector_version).eq("status",status)
        if start is not None:q=q.gte("window_start",start.isoformat())
        if end is not None:q=q.lt("window_start",end.isoformat())
        rows+=q.order("queued_at").limit(limit).execute().data
    return rows[:limit]

def _open_jobs_for_selector(sb,selector_version,*,start=None,end=None,before=None,limit=4):
    """같은 selector 의 queued|failed_retryable 만 조회. status query 를 합친 뒤 Python 에서
    queued_at 오름차순으로 안정 정렬해 status append 순서에 의존하지 않게 한다."""
    rows=[]
    for status in ("queued","failed_retryable"):
        q=sb.table("clip_vlm_jobs").select("*").eq("selector_version",selector_version).eq("status",status)
        if start is not None:q=q.gte("window_start",start.isoformat())   # inclusive
        if end is not None:q=q.lt("window_start",end.isoformat())        # exclusive
        if before is not None:q=q.lt("window_start",before.isoformat())  # current window_start exclusive
        rows+=q.order("queued_at").limit(limit).execute().data
    # 짧은 영상 자동 제외(설계 §6): quarantined/media_deleted clip 의 due/recovery job 은 반환하지 않는다.
    # 기존 clip_vlm_jobs row 는 읽기만 하고 update/delete 하지 않는다.
    excluded=load_system_excluded_clip_ids(sb,[r.get("clip_id") for r in rows])
    if excluded:rows=[r for r in rows if r.get("clip_id") not in excluded]
    rows.sort(key=lambda r:(r.get("queued_at") or ""))
    return rows[:limit]

def load_due_jobs_for_selector_window(sb,selector_version,start,end,limit=4):
    return _open_jobs_for_selector(sb,selector_version,start=start,end=end,limit=limit)

def load_recovery_jobs_for_selector(sb,selector_version,before,limit=4):
    return _open_jobs_for_selector(sb,selector_version,before=before,limit=limit)
