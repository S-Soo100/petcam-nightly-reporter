import json
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import cv2,numpy as np,pytest
from tests._fakes import FakeSB
from reporter.vlm_frames import normalize_jpeg,sample_times
from reporter.vlm_store import (create_run_and_jobs,load_due_jobs_for_selector_window,
                                load_recovery_jobs_for_selector,mark_submitted)
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION
from reporter.anthropic_analyzer import analyze_clip
from reporter.vlm_models import CandidateClip

_REG = "budget-router-v1"
_START = datetime(2026,7,16,0,tzinfo=timezone.utc)
_END = datetime(2026,7,16,2,tzinfo=timezone.utc)


def _job(job_id,selector,window_start,status,queued_at):
    return {"id":job_id,"selector_version":selector,"window_start":window_start,
            "status":status,"queued_at":queued_at,"attempt_count":0}


def _queue_store():
    # current window [00:00,02:00), old window at 07-15T22:30. queued_at 을 status append
    # 순서와 어긋나게 배치해 정렬이 status 순서가 아니라 queued_at 임을 검증.
    return FakeSB({"clip_vlm_jobs":[
        _job("cur-q",_REG,"2026-07-16T00:30:00+00:00","queued","2026-07-16T00:00:05+00:00"),
        _job("cur-r",_REG,"2026-07-16T01:00:00+00:00","failed_retryable","2026-07-16T00:00:01+00:00"),
        _job("old-r",_REG,"2026-07-15T22:30:00+00:00","failed_retryable","2026-07-15T22:00:00+00:00"),
        _job("bf-cur-q",BACKFILL_SELECTOR_VERSION,"2026-07-16T00:45:00+00:00","queued","2026-07-16T00:00:03+00:00"),
        _job("bf-old-r",BACKFILL_SELECTOR_VERSION,"2026-07-15T22:00:00+00:00","failed_retryable","2026-07-15T21:00:00+00:00"),
        _job("done",_REG,"2026-07-16T00:15:00+00:00","succeeded","2026-07-16T00:00:00+00:00"),
        _job("term",_REG,"2026-07-16T00:20:00+00:00","failed_terminal","2026-07-16T00:00:00+00:00"),
        _job("held",_REG,"2026-07-16T00:25:00+00:00","held_model_mismatch","2026-07-16T00:00:00+00:00"),
    ]})


def test_current_window_loader_returns_only_same_selector_window_and_open_status():
    sb=_queue_store()
    due=load_due_jobs_for_selector_window(sb,_REG,_START,_END)
    assert [j["id"] for j in due]==["cur-r","cur-q"]  # queued_at 오름차순 안정 정렬


def test_recovery_loader_returns_only_older_same_selector_open_jobs():
    sb=_queue_store()
    rec=load_recovery_jobs_for_selector(sb,_REG,before=_START)
    assert [j["id"] for j in rec]==["old-r"]


def test_queue_loaders_never_return_backfill_terminal_or_held():
    sb=_queue_store()
    ids=set()
    ids|={j["id"] for j in load_due_jobs_for_selector_window(sb,_REG,_START,_END)}
    ids|={j["id"] for j in load_recovery_jobs_for_selector(sb,_REG,before=_START)}
    assert ids=={"cur-r","cur-q","old-r"}
    assert not (ids & {"bf-cur-q","bf-old-r","done","term","held"})


def test_queue_loaders_respect_limit():
    rows=[_job(f"q{i}",_REG,"2026-07-16T00:30:00+00:00","queued",f"2026-07-16T00:00:{i:02d}+00:00") for i in range(6)]
    rows+=[_job(f"o{i}",_REG,"2026-07-15T22:30:00+00:00","failed_retryable",f"2026-07-15T22:00:{i:02d}+00:00") for i in range(6)]
    sb=FakeSB({"clip_vlm_jobs":rows})
    assert len(load_due_jobs_for_selector_window(sb,_REG,_START,_END,limit=4))==4
    assert len(load_recovery_jobs_for_selector(sb,_REG,before=_START,limit=4))==4

def test_frames_are_six_and_never_upscale(tmp_path):
    assert sample_times(30)==[2.5,7.5,12.5,17.5,22.5,27.5]
    p=tmp_path/"x.jpg";cv2.imwrite(str(p),np.zeros((1080,1920,3),dtype=np.uint8))
    assert normalize_jpeg(p)==(768,432)

def test_atomic_store_and_budget_reservation():
    sb=FakeSB();run={"camera_id":"cam","window_start":"s","selector_version":"v"};jobs=[{"clip_id":"c","slot":"customer_highlight"}]
    assert create_run_and_jobs(sb,run,jobs)
    job=sb.store["clip_vlm_jobs"][0]
    assert mark_submitted(sb,job,"2026-07-01T00:00:00Z",10) is True
    assert job["status"]=="submitted"

class Messages:
    def __init__(self):self.calls=[]
    def create(self,**kw):
        self.calls.append(kw)
        usage=SimpleNamespace(input_tokens=100,cache_creation_input_tokens=0,cache_read_input_tokens=0,output_tokens=10)
        return SimpleNamespace(id="m1",model="claude-sonnet-5",usage=usage,content=[SimpleNamespace(type="text",text=json.dumps({"action":"moving","confidence":.8,"reasoning":"moves"}))])
def test_analyzer_sends_six_images_and_exact_model(tmp_path):
    paths=[]
    for i in range(6):
        p=tmp_path/f"{i}.jpg";p.write_bytes(b"jpeg");paths.append(p)
    client=SimpleNamespace(messages=Messages());c=CandidateClip("c","cam",datetime.now(timezone.utc),30,"k",1,1280,720)
    r=analyze_clip(client,paths,c,"claude-sonnet-5")
    assert r.model_actual=="claude-sonnet-5"
    assert sum(b["type"]=="image" for b in client.messages.calls[0]["messages"][0]["content"])==6
    with pytest.raises(ValueError):analyze_clip(client,paths,c,"sonnet")
