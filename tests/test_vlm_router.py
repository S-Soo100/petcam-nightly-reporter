from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from reporter.vlm_budget import Usage, calculate_cost, fair_job_order
from reporter.vlm_episode import bbox_bucket, reduce_episodes
from reporter.vlm_models import CandidateClip, Slot
from reporter.vlm_selector import select_candidates
from reporter.timewin import trigger_window


def clip(cid, sec, decision="active", score=1.0, bbox=(0, 0, 100, 100), roi=1.0, bg=0.1):
    return CandidateClip(cid, "cam", datetime(2026, 7, 14, 11, tzinfo=timezone.utc)+timedelta(seconds=sec),
        30.0, f"clips/{cid}.mp4", score, 1280, 720, activity_decision=decision,
        prelabel_id="p", gecko_visible=True, gecko_bbox=bbox,
        motion_metrics={"roi_flow_mag": roi, "global_bg_change": bg, "max_bbox_center_disp": 0.1})


def test_trigger_window_four_runs():
    for hour, start_hour in ((22,20),(0,22),(2,0),(4,2)):
        now=datetime(2026,7,15 if hour else 16,hour,7,tzinfo=ZoneInfo("Asia/Seoul"))
        start,end=trigger_window(now)
        assert end-start==timedelta(hours=2)
        assert start.astimezone(ZoneInfo("Asia/Seoul")).hour==start_hour


def test_episode_and_bbox_dedup():
    assert bbox_bucket((0,0,100,100),1280,720)==(0,0,"small")
    assert len(reduce_episodes([clip("a",0),clip("b",110)], datetime(2026,7,14,11,tzinfo=timezone.utc)))==1
    assert len(reduce_episodes([clip("a",0),clip("b",121)], datetime(2026,7,14,11,tzinfo=timezone.utc)))==2


def test_four_slots_are_unique_and_audit_keeps_exclusions():
    clips=[clip("h",0,roi=4),clip("s",200,score=.1,roi=2,bg=.01),clip("d",400,decision="unknown"),clip("a",600,decision="exclude_absent"),clip("x",800,decision="exclude_static")]
    selected=select_candidates(reduce_episodes(clips,clips[0].started_at),{},clips[0].started_at)
    assert len({s.clip.id for s in selected})==len(selected)
    assert any(s.slot is Slot.EXCLUSION_AUDIT and s.clip.activity_decision.startswith("exclude") for s in selected)


def test_cost_and_fair_order():
    assert calculate_cost(Usage(7000,3000,2000,100))==Decimal("0.022900")
    jobs=[{"camera_id":"a","slot":"subtle_behavior"},{"camera_id":"b","slot":"customer_highlight"},{"camera_id":"a","slot":"customer_highlight"}]
    assert [(j["camera_id"],j["slot"]) for j in fair_job_order(jobs)]==[("a","customer_highlight"),("b","customer_highlight"),("a","subtle_behavior")]
