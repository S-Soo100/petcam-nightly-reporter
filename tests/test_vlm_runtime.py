import json
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import cv2,numpy as np,pytest
from tests._fakes import FakeSB
from reporter.vlm_frames import normalize_jpeg,sample_times
from reporter.vlm_store import create_run_and_jobs,mark_submitted
from reporter.anthropic_analyzer import analyze_clip
from reporter.vlm_models import CandidateClip

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
