"""Metadata-only selector replay. No R2 download and no API call."""
import argparse,json
from datetime import datetime
from supabase import create_client
from reporter import config
from reporter.timewin import trigger_window
from reporter.vlm_candidate_indexer import load_window_candidates,partition_eligibility
from reporter.vlm_episode import reduce_episodes
from reporter.vlm_selector import select_candidates
def main():
    p=argparse.ArgumentParser();p.add_argument("--window-end",action="append",required=True);a=p.parse_args();sb=create_client(config.SUPABASE_URL,config.SUPABASE_KEY);out=[]
    for raw in a.window_end:
        now=datetime.fromisoformat(raw);start,end=trigger_window(now);clips=load_window_candidates(sb,start,end,config.VLM_ACTIVITY_POLICY_VERSION,config.VLM_SELECTOR_VERSION)
        for cam in sorted({c.camera_id for c in clips}):
            eligible,_=partition_eligibility([c for c in clips if c.camera_id==cam]);sel=select_candidates(reduce_episodes(eligible,start),{},start);out += [{"camera_id":cam,"window_start":start.isoformat(),"slot":s.slot.value,"clip_id":s.clip.id,"episode":s.episode_key} for s in sel]
    assert all(sum(r["camera_id"]==c and r["window_start"]==w for r in out)<=4 for c,w in {(r["camera_id"],r["window_start"]) for r in out});print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
