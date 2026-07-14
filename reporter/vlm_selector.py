import hashlib, statistics
from reporter.vlm_models import Slot,SelectedCandidate

ORDER=(Slot.CUSTOMER_HIGHLIGHT,Slot.SUBTLE_BEHAVIOR,Slot.DIVERSITY_DISCOVERY,Slot.EXCLUSION_AUDIT)

def _m(c,k):
    try:return float(c.motion_metrics.get(k,0) or 0)
    except (TypeError,ValueError):return 0.0

def _bucket(r):
    return f"{r.clip.camera_id}|{r.clip.started_at.hour//2}|{r.clip.activity_decision or 'unknown'}|{r.motion_quartile}|{r.bbox_bucket}"

def select_candidates(reps,history,window_start):
    remaining=list(reps); chosen=[]
    roi_med=statistics.median([_m(r.clip,"roi_flow_mag") for r in reps]) if reps else 0
    bg_med=statistics.median([_m(r.clip,"global_bg_change") for r in reps]) if reps else 0
    for slot in ORDER:
        if slot is Slot.CUSTOMER_HIGHLIGHT:
            # prelabel 존재 자체는 활동 근거가 아니다. absent/static evidence도 prelabel을 가지므로
            # customer highlight에는 activity policy가 active인 episode만 넣는다.
            pool=[r for r in remaining if r.clip.activity_decision=="active"]
            score=lambda r:(r.clip.activity_decision=="active",_m(r.clip,"roi_flow_mag"),_m(r.clip,"max_bbox_center_disp"),-history.get(_bucket(r),0),r.clip.motion_score)
        elif slot is Slot.SUBTLE_BEHAVIOR:
            pool=[r for r in remaining if r.clip.gecko_bbox and _m(r.clip,"global_bg_change")<=bg_med and _m(r.clip,"roi_flow_mag")>=roi_med]
            score=lambda r:(_m(r.clip,"roi_flow_mag")/(_m(r.clip,"global_bg_change")+.001),-history.get(_bucket(r),0))
        elif slot is Slot.DIVERSITY_DISCOVERY:
            pool=remaining; score=lambda r:(-history.get(_bucket(r),0),-r.motion_quartile)
        else:
            pool=[r for r in remaining if (r.clip.activity_decision or "unknown") in {"exclude_absent","exclude_static","unknown"}]
            score=lambda r:(-history.get(_bucket(r),0),hashlib.sha256(f"{window_start}:{r.clip.id}".encode()).hexdigest())
        if not pool:continue
        item=max(pool,key=score); features={"diversity_bucket":_bucket(item),"motion_quartile":item.motion_quartile,"bbox_bucket":item.bbox_bucket,"activity_decision":item.clip.activity_decision}
        chosen.append(SelectedCandidate(item.clip,slot,item.episode_key,features,f"{slot.value}:{score(item)}"))
        remaining=[r for r in remaining if r.clip.id!=item.clip.id and r.episode_key!=item.episode_key]
    return chosen
