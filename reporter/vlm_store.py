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

_JOB_PAGE=100        # keyset 페이지 크기(짧은 영상 제외가 앞을 많이 차지해도 뒤 eligible 확보).
_JOB_MAX_PAGES=200   # 무한 루프 backstop.

def _open_jobs_for_selector(sb,selector_version,*,start=None,end=None,before=None,limit=4,page=_JOB_PAGE):
    """같은 selector 의 queued|failed_retryable 을 (queued_at ASC, id ASC) 복합 keyset 으로 조회.

    queued_at 만으로는 동률 행 순서가 비결정적이라 offset 페이지네이션이 경계에서 중복/누락된다
    (production 실측: 689행 중 495행이 duplicate timestamp, max 동률 4). id 를 tiebreak 로 더해
    다음 페이지 조건을 `queued_at > last_queued_at OR (queued_at = last_queued_at AND id > last_id)`
    로 둔다(= (queued_at,id) 튜플 strict 비교). offset range 는 쓰지 않는다.

    짧은 영상 자동 제외(설계 §6): quarantined/media_deleted clip 의 job 은 건너뛴다. **앞의 제외 job
    수와 무관하게 뒤의 eligible job 을 limit 만큼** 채운다. 기존 clip_vlm_jobs row 는 읽기만 한다
    (update/delete 0).
    """
    page=max(page,1)
    eligible=[]
    last_qa=None  # 직전 페이지 마지막 행의 queued_at
    last_id=None  # 직전 페이지 마지막 행의 id (동률 tiebreak)
    for _ in range(_JOB_MAX_PAGES):
        q=sb.table("clip_vlm_jobs").select("*").eq("selector_version",selector_version).in_("status",["queued","failed_retryable"])
        if start is not None:q=q.gte("window_start",start.isoformat())   # inclusive
        if end is not None:q=q.lt("window_start",end.isoformat())        # exclusive
        if before is not None:q=q.lt("window_start",before.isoformat())  # current window_start exclusive
        if last_qa is not None:q=q.gte("queued_at",last_qa)              # keyset 하한(동률 id 는 아래 strict)
        rows=q.order("queued_at").execute().data
        # 복합 정렬 (queued_at ASC, id ASC): 동률 queued_at 을 id 로 안정 tiebreak.
        rows=sorted(rows,key=lambda r:(r.get("queued_at") or "",r.get("id") or ""))
        if last_qa is not None:
            # 다음 페이지 조건: queued_at > last_qa OR (queued_at = last_qa AND id > last_id).
            rows=[r for r in rows if (r.get("queued_at") or "",r.get("id") or "")>(last_qa,last_id)]
        if not rows:break
        batch=rows[:page]
        excluded=load_system_excluded_clip_ids(sb,[r.get("clip_id") for r in batch])
        for r in batch:
            if r.get("clip_id") not in excluded:
                eligible.append(r)
                if len(eligible)>=limit:return eligible[:limit]
        last=batch[-1];last_qa=last.get("queued_at") or "";last_id=last.get("id") or ""
        if len(batch)>=len(rows):break  # 이번 fetch 를 다 소비 = 더 없음
    return eligible[:limit]

def load_due_jobs_for_selector_window(sb,selector_version,start,end,limit=4):
    return _open_jobs_for_selector(sb,selector_version,start=start,end=end,limit=limit)

def load_recovery_jobs_for_selector(sb,selector_version,before,limit=4):
    return _open_jobs_for_selector(sb,selector_version,before=before,limit=limit)
