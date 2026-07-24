from collections import Counter
from datetime import datetime
from reporter.vlm_models import CandidateClip
from reporter.short_clip_retention_store import load_system_excluded_clip_ids

def _chunks(values,n=200):
    for i in range(0,len(values),n):yield values[i:i+n]
def _rows(sb,table,col,ids,extra=None):
    out=[]
    for chunk in _chunks(ids):
        q=sb.table(table).select("*").in_(col,chunk);q=extra(q) if extra else q;out+=q.execute().data
    return out
def load_window_candidates(sb,start,end,policy_version,selector_version):
    rows=sb.table("motion_clips").select("id,camera_id,started_at,duration_sec,r2_key,motion_score,width,height").gte("started_at",start.isoformat()).lt("started_at",end.isoformat()).order("started_at").execute().data
    ids=[r["id"] for r in rows]
    if not ids:return []
    # 짧은 영상 자동 제외(설계 §6): quarantined/media_deleted 는 새 VLM 후보 선정 전에 걸러낸다.
    excluded=load_system_excluded_clip_ids(sb,ids)
    if excluded:
        rows=[r for r in rows if r["id"] not in excluded]
        ids=[r["id"] for r in rows]
        if not ids:return []
    ass=_rows(sb,"clip_activity_assessments","clip_id",ids,lambda q:q.eq("policy_version",policy_version)); amap={r["clip_id"]:r for r in ass}
    pre=_rows(sb,"clip_prelabels","id",[r["prelabel_id"] for r in ass]);pmap={r["id"]:r for r in pre}
    jobs=_rows(sb,"clip_vlm_jobs","clip_id",ids);jmap={}
    for j in jobs:jmap.setdefault(j["clip_id"],[]).append(j)
    out=[]
    for r in rows:
        a=amap.get(r["id"]);p=pmap.get(a.get("prelabel_id")) if a else None;reason=None
        for j in jmap.get(r["id"],[]):
            if j.get("selector_version")==selector_version:reason="already_selector_job"
            if j.get("status")=="succeeded" and j.get("model_requested")=="claude-sonnet-5" and j.get("prompt_version")=="v4.0-direct-images" and j.get("sampler_version")=="six-768q85-v1":reason="already_analyzed_identity"
        bbox=tuple(p["gecko_bbox"]) if p and p.get("gecko_bbox") else None
        out.append(CandidateClip(r["id"],r["camera_id"],datetime.fromisoformat(r["started_at"].replace("Z","+00:00")),float(r["duration_sec"]),r.get("r2_key"),float(r.get("motion_score") or 0),r.get("width"),r.get("height"),a.get("id") if a else None,a.get("decision") if a else None,p.get("id") if p else None,p.get("gecko_visible") if p else None,p.get("visibility_confidence") if p else None,bbox,p.get("motion_metrics") or {} if p else {},reason))
    return out
def partition_eligibility(clips):
    ok=[];reasons=Counter()
    for c in clips:
        if c.duration_sec<=0 or not c.r2_key:reasons["invalid_input"]+=1
        elif c.hard_skip_reason:reasons[c.hard_skip_reason]+=1
        else:ok.append(c)
    return ok,dict(reasons)
def load_recent_history(sb,camera_id,since):
    rows=sb.table("clip_vlm_jobs").select("rank_features").eq("camera_id",camera_id).gte("created_at",since.isoformat()).execute().data
    return dict(Counter((r.get("rank_features") or {}).get("diversity_bucket") for r in rows if (r.get("rank_features") or {}).get("diversity_bucket")))
