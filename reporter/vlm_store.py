def create_run_and_jobs(sb,run,jobs):
    if len(jobs)>4:raise ValueError("max 4 jobs")
    return sb.rpc("fn_create_clip_vlm_selector_run",{"p_run":run,"p_jobs":jobs}).execute().data
def mark_submitted(sb,job,month_start,budget):
    return bool(sb.rpc("fn_reserve_clip_vlm_job",{"p_job_id":job["id"],"p_month_start":month_start,"p_budget_usd":str(budget)}).execute().data)
def update_job(sb,job_id,values):
    rows=sb.table("clip_vlm_jobs").update(values).eq("id",job_id).execute().data
    if len(rows)!=1:raise RuntimeError("job update failed")
    return rows[0]
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
