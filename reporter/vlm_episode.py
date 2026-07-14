import bisect, hashlib, math
from reporter.vlm_models import CandidateClip, EpisodeRepresentative


def bbox_bucket(bbox,width,height):
    if bbox is None or not width or not height:return ("none","none","none")
    x,y,w,h=bbox; col=min(2,max(0,int(((x+w/2)/width)*3))); row=min(2,max(0,int(((y+h/2)/height)*3)))
    ratio=(w*h)/(width*height); size="small" if ratio<.05 else "medium" if ratio<.2 else "large"
    return row,col,size


def _quartiles(clips):
    vals=sorted(c.motion_score for c in clips); n=len(vals)
    cuts=[vals[max(0,math.ceil(n*p)-1)] for p in (.25,.5,.75)] if vals else [0,0,0]
    return {c.id:bisect.bisect_right(cuts,c.motion_score) for c in clips}


def reduce_episodes(clips:list[CandidateClip],window_start):
    if not clips:return []
    clips=sorted(clips,key=lambda c:(c.started_at,c.id)); qs=_quartiles(clips); groups=[]; current=[]
    for c in clips:
        key=(c.activity_decision or "unknown",qs[c.id],bbox_bucket(c.gecko_bbox,c.width,c.height))
        if current:
            p=current[-1]; pkey=(p.activity_decision or "unknown",qs[p.id],bbox_bucket(p.gecko_bbox,p.width,p.height))
            if (c.started_at-p.started_at).total_seconds()>120 or key!=pkey:
                groups.append(current); current=[]
        current.append(c)
    groups.append(current); out=[]
    for group in groups:
        mid=group[len(group)//2].started_at
        rep=max(group,key=lambda c:(int(c.prelabel_id is not None and bool(c.motion_metrics)),int(c.duration_sec>0 and c.r2_key is not None),-abs((c.started_at-mid).total_seconds()),hashlib.sha256(f"{c.camera_id}:{window_start}:{c.id}".encode()).hexdigest()))
        b=bbox_bucket(rep.gecko_bbox,rep.width,rep.height); raw=f"{rep.camera_id}|{group[0].started_at.isoformat()}|{rep.activity_decision}|{qs[rep.id]}|{b}"
        out.append(EpisodeRepresentative(rep,hashlib.sha256(raw.encode()).hexdigest()[:24],qs[rep.id],b))
    return out
