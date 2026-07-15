from datetime import datetime
from zoneinfo import ZoneInfo
from tests._fakes import FakeSB
from reporter.claude_cli_analyzer import CliBatchError, CliBatchResult
from reporter.vlm_budget import Usage
from reporter.vlm_candidate_worker import cap_night_selections, failure_status, process_cli_jobs, run
from reporter.vlm_models import CandidateClip, SelectedCandidate, Slot

def test_disabled_worker_does_not_touch_db():
    sb=FakeSB();assert run(sb=sb,now=datetime(2026,7,15,22,tzinfo=ZoneInfo("Asia/Seoul")),enabled=False)==0
    assert sb.store=={}


def _selected(camera_id, slot, clip_id):
    clip=CandidateClip(clip_id,camera_id,datetime(2026,7,15,22,tzinfo=ZoneInfo("Asia/Seoul")),30,"k",1,1280,720)
    return SelectedCandidate(clip,slot,clip_id,{},slot.value)


def test_night_cap_is_fair_and_respects_camera_limit():
    selected={
        "A":[_selected("A",s,f"a{i}") for i,s in enumerate(Slot)],
        "B":[_selected("B",s,f"b{i}") for i,s in enumerate(Slot)],
    }
    capped=cap_night_selections(selected,{"A":15,"B":0},3)
    assert [x.slot for x in capped["A"]]==[Slot.CUSTOMER_HIGHLIGHT]
    assert [x.slot for x in capped["B"]]==[Slot.CUSTOMER_HIGHLIGHT,Slot.SUBTLE_BEHAVIOR]


def test_failure_status_allows_exactly_two_attempts():
    assert failure_status({"attempt_count":0})=="failed_retryable"
    assert failure_status({"attempt_count":1})=="failed_terminal"
    assert failure_status({"attempt_count":2})=="failed_terminal"


def test_cli_provider_batches_four_jobs_and_splits_usage(tmp_path):
    jobs=[]; clips=[]
    for index in range(4):
        clip_id=f"c{index}"
        jobs.append({"id":f"j{index}","clip_id":clip_id,"camera_id":"cam","selector_run_id":"run","slot":list(Slot)[index].value,"status":"queued","attempt_count":0})
        clips.append({"id":clip_id,"camera_id":"cam","started_at":"2026-07-15T13:00:00+00:00","duration_sec":30,"r2_key":f"{clip_id}.mp4","motion_score":1,"width":1280,"height":720})
    sb=FakeSB({"clip_vlm_jobs":jobs,"motion_clips":clips})
    calls=[]

    def analyzer(frame_sets, model):
        calls.append(set(frame_sets))
        results={clip_id:{"clip_id":clip_id,"action":"moving","confidence":.8,"reasoning":"moves"} for clip_id in frame_sets}
        return CliBatchResult("session","claude-sonnet-5","claude-sonnet-5",results,Usage(101,41,31,21),.12,False)

    stats=process_cli_jobs(
        sb,jobs,analyzer=analyzer,
        auth_check=lambda:None,
        download_fn=lambda _key,dest: dest,
        extract_fn=lambda _video,out: [out/f"{i}.jpg" for i in range(6)],
    )
    assert len(calls)==1 and calls[0]=={"c0","c1","c2","c3"}
    assert stats=={"succeeded":4}
    stored=sb.store["clip_vlm_jobs"]
    assert sum(row["input_tokens"] for row in stored)==101
    assert sum(row["output_tokens"] for row in stored)==21
    assert all(row["cost_usd"]=="0" for row in stored)
    assert all(row["result"]["provider"]=="claude_cli_batch" for row in stored)


def test_cli_provider_keeps_processing_when_one_clip_download_fails(tmp_path):
    jobs=[
        {"id":"j0","clip_id":"c0","camera_id":"cam","selector_run_id":"run","slot":Slot.CUSTOMER_HIGHLIGHT.value,"status":"queued","attempt_count":0},
        {"id":"j1","clip_id":"c1","camera_id":"cam","selector_run_id":"run","slot":Slot.SUBTLE_BEHAVIOR.value,"status":"queued","attempt_count":0},
    ]
    clips=[{"id":clip_id,"r2_key":f"{clip_id}.mp4"} for clip_id in ("c0","c1")]
    sb=FakeSB({"clip_vlm_jobs":jobs,"motion_clips":clips})

    def download(key,dest):
        if key=="c0.mp4":raise OSError("broken")
        return dest

    def analyzer(frame_sets,model):
        assert set(frame_sets)=={"c1"}
        item={"clip_id":"c1","action":"moving","confidence":.8,"reasoning":"moves"}
        return CliBatchResult("session",model,model,{"c1":item},Usage(10,0,0,2),.01,False)

    stats=process_cli_jobs(
        sb,jobs,analyzer=analyzer,auth_check=lambda:None,download_fn=download,
        extract_fn=lambda _video,out:[out/f"{i}.jpg" for i in range(6)],
    )
    assert stats=={"failed_retryable":1,"succeeded":1}


def test_cli_provider_auth_failure_preserves_retry_attempt_and_records_safe_code():
    job={"id":"j0","clip_id":"c0","camera_id":"cam","selector_run_id":"run","slot":Slot.CUSTOMER_HIGHLIGHT.value,"status":"failed_retryable","attempt_count":1}
    sb=FakeSB({"clip_vlm_jobs":[job],"motion_clips":[{"id":"c0","r2_key":"c0.mp4"}]})

    def auth_check():
        raise CliBatchError("not_logged_in")

    def must_not_run(*_args,**_kwargs):
        raise AssertionError("auth failure must stop before download/analyzer")

    stats=process_cli_jobs(
        sb,[job],auth_check=auth_check,analyzer=must_not_run,
        download_fn=must_not_run,extract_fn=must_not_run,
    )
    stored=sb.store["clip_vlm_jobs"][0]
    assert stats=={"not_logged_in":1}
    assert stored["status"]=="failed_retryable"
    assert stored["attempt_count"]==1
    assert stored["error_code"]=="not_logged_in"
