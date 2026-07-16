import json
from datetime import datetime
from zoneinfo import ZoneInfo
from tests._fakes import FakeSB
from reporter import config
from reporter.claude_cli_analyzer import CliBatchError, CliBatchResult, clip_failure_diagnostic
from reporter.vlm_budget import Usage
from reporter.vlm_candidate_worker import breaker_triggered, cap_night_selections, failure_status, process_cli_jobs, run
from reporter.vlm_models import CandidateClip, SelectedCandidate, Slot

_TRIGGER = datetime(2026, 7, 16, 2, tzinfo=ZoneInfo("Asia/Seoul"))


def _run_with_seams(current_batches, remaining_after, recovery_jobs, *, breaker=None, sb=None,
                    acquire_lock_fn=lambda: object(), send_fn=None):
    """빈 sb 로 candidate run 을 돌리되 loader/process/lock/send seam 만 주입해 순서를 검증한다."""
    calls = {"current": [], "recovery": [], "process": [], "selectors": [], "sent": []}
    stats = {"succeeded": 0} if breaker is None else {breaker: 1}

    def load_current_fn(_sb, selector, start, end, limit=4):
        calls["current"].append(limit); calls["selectors"].append(selector)
        return current_batches if len(calls["current"]) == 1 else remaining_after

    def load_recovery_fn(_sb, selector, before, limit=4):
        calls["recovery"].append((before, limit)); calls["selectors"].append(selector)
        return recovery_jobs

    def process_fn(_sb, jobs):
        calls["process"].append([j["id"] for j in jobs]); return dict(stats)

    def default_send(text):
        calls["sent"].append(text); return True

    rc = run(sb=sb or FakeSB(), now=_TRIGGER, enabled=True, process_fn=process_fn,
             load_current_fn=load_current_fn, load_recovery_fn=load_recovery_fn,
             expected_host="test-host", hostname_fn=lambda: "test-host",
             acquire_lock_fn=acquire_lock_fn, release_lock_fn=lambda _l: None,
             send_fn=send_fn or default_send)
    return rc, calls


def test_breaker_triggered_reads_stats_codes():
    assert breaker_triggered({"not_logged_in": 1}) is True
    assert breaker_triggered({"quota_exceeded": 1}) is True
    assert breaker_triggered({"held_model_mismatch": 1}) is True
    assert breaker_triggered({"clip_set_mismatch": 1}) is True
    assert breaker_triggered({"succeeded": 4}) is False


def test_candidate_processes_current_then_bounded_recovery():
    rc, calls = _run_with_seams([{"id": "cur1"}], [], [{"id": "old1"}, {"id": "old2"}])
    assert rc == 0
    assert calls["process"] == [["cur1"], ["old1", "old2"]]
    assert calls["recovery"][0][1] == config.VLM_MAX_PER_CAMERA_WINDOW
    assert set(calls["selectors"]) == {config.VLM_SELECTOR_VERSION}


def test_candidate_skips_recovery_when_current_window_not_drained():
    rc, calls = _run_with_seams([{"id": "cur1"}], [{"id": "cur1"}], [{"id": "old1"}])
    assert calls["process"] == [["cur1"]]
    assert calls["recovery"] == []


def test_candidate_skips_recovery_on_breaker():
    rc, calls = _run_with_seams([{"id": "cur1"}], [], [{"id": "old1"}], breaker="not_logged_in")
    assert calls["process"] == [["cur1"]]
    assert calls["recovery"] == []


def test_candidate_runs_with_no_current_jobs():
    rc, calls = _run_with_seams([], [], [])
    assert rc == 0
    assert calls["process"][0] == []


def test_run_sends_exactly_one_vlm_summary_on_success():
    rc, calls = _run_with_seams([{"id": "cur1"}], [], [])
    assert rc == 0
    assert len(calls["sent"]) == 1
    assert "VLM 행동 분석" in calls["sent"][0]


def test_run_blocked_lock_sends_warning_and_returns_nonzero_without_processing():
    rc, calls = _run_with_seams([], [], [], acquire_lock_fn=lambda: None)
    assert rc != 0
    assert calls["process"] == []  # lock 실패 시 DB/Claude 0회
    assert len(calls["sent"]) == 1 and "blocked_lock" in calls["sent"][0]


def test_run_slack_failure_does_not_recall_process_or_break():
    def failing_send(_text):
        raise RuntimeError("slack down")
    rc, calls = _run_with_seams([{"id": "cur1"}], [{"id": "cur1"}], [], send_fn=failing_send)
    assert rc == 0
    assert calls["process"] == [["cur1"]]  # Slack 실패 후 process/recovery 재호출 없음


def test_run_dedups_slack_across_same_window_reruns():
    sb = FakeSB()
    _, c1 = _run_with_seams([{"id": "cur1"}], [{"id": "cur1"}], [], sb=sb)
    _, c2 = _run_with_seams([{"id": "cur1"}], [{"id": "cur1"}], [], sb=sb)
    assert len(c1["sent"]) == 1  # 최초 window → 1회 전송
    assert len(c2["sent"]) == 0  # 같은 scheduled window 재실행 → durable dedup


def test_run_slack_failure_releases_claim_so_next_run_resends():
    sb = FakeSB()

    def failing_send(_text):
        raise RuntimeError("slack down")

    _, c1 = _run_with_seams([{"id": "cur1"}], [{"id": "cur1"}], [], sb=sb, send_fn=failing_send)
    _, c2 = _run_with_seams([{"id": "cur1"}], [{"id": "cur1"}], [], sb=sb)  # 재전송
    assert len(c2["sent"]) == 1  # 전송 실패로 claim 해제 → 다음 실행이 재전송


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
    assert stats.counts=={"succeeded":4} and stats.breaker is None
    assert all(row["failure_diagnostic"] is None for row in sb.store["clip_vlm_jobs"])  # 첫 시도 성공 = diagnostic null
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
    assert stats.counts=={"failed_retryable":1,"succeeded":1} and stats.breaker is None


def test_cli_provider_auth_failure_preserves_retry_attempt_and_records_safe_code():
    job={"id":"j0","clip_id":"c0","camera_id":"cam","selector_run_id":"run","slot":Slot.CUSTOMER_HIGHLIGHT.value,"status":"failed_retryable","attempt_count":1}
    sb=FakeSB({"clip_vlm_jobs":[job],"motion_clips":[{"id":"c0","r2_key":"c0.mp4"}]})

    def auth_check():
        raise CliBatchError("not_logged_in", disposition="breaker",
                            diagnostic=clip_failure_diagnostic(RuntimeError("x"), phase="auth"))

    def must_not_run(*_args,**_kwargs):
        raise AssertionError("auth failure must stop before download/analyzer")

    stats=process_cli_jobs(
        sb,[job],auth_check=auth_check,analyzer=must_not_run,
        download_fn=must_not_run,extract_fn=must_not_run,
    )
    stored=sb.store["clip_vlm_jobs"][0]
    assert stats.counts=={"not_logged_in":1} and stats.breaker=="auth"
    assert stored["status"]=="failed_retryable"
    assert stored["attempt_count"]==1
    assert stored["error_code"]=="not_logged_in"
    assert stored["failure_diagnostic"]["phase"]=="auth"


# --- Task 5: ProcessResult breaker/diagnostic contract ---

def _cli_job(job_id, clip_id, camera, run_id, slot):
    return {"id": job_id, "clip_id": clip_id, "camera_id": camera, "selector_run_id": run_id,
            "slot": slot, "status": "queued", "attempt_count": 0}


def _ok_batch(frame_sets, model="claude-sonnet-5", mismatch_model=None):
    results = {cid: {"clip_id": cid, "action": "moving", "confidence": .8, "reasoning": "m"} for cid in frame_sets}
    actual = mismatch_model or model
    return CliBatchResult("s", model, actual, results, Usage(10, 0, 0, 2), .01, mismatch_model is not None)


def _six(_video, out):
    return [out / f"{i}.jpg" for i in range(6)]


def _two_camera_sb():
    jobs = [_cli_job("j0", "c0", "camA", "runA", Slot.CUSTOMER_HIGHLIGHT.value),
            _cli_job("j1", "c1", "camB", "runB", Slot.CUSTOMER_HIGHLIGHT.value)]
    clips = [{"id": "c0", "r2_key": "c0.mp4"}, {"id": "c1", "r2_key": "c1.mp4"}]
    return FakeSB({"clip_vlm_jobs": jobs, "motion_clips": clips}), jobs


def test_cli_provider_auth_breaker_skips_all_batches():
    sb, jobs = _two_camera_sb()

    def boom(*_a, **_k):
        raise AssertionError("nothing must run after auth breaker")

    def auth_fail():
        raise CliBatchError("not_logged_in", disposition="breaker")

    res = process_cli_jobs(sb, jobs, analyzer=boom, auth_check=auth_fail, download_fn=boom, extract_fn=boom)
    assert res.breaker == "auth"
    assert res.counts == {"not_logged_in": 2}


def test_cli_provider_model_mismatch_holds_and_stops_further_batches():
    sb, jobs = _two_camera_sb()
    calls = []

    def analyzer(frame_sets, model):
        calls.append(set(frame_sets))
        return _ok_batch(frame_sets, mismatch_model="claude-sonnet-4-6")

    res = process_cli_jobs(sb, jobs, analyzer=analyzer, auth_check=lambda: None, download_fn=lambda _k, d: d, extract_fn=_six)
    assert len(calls) == 1  # 2번째 camera batch 는 호출 0
    assert res.breaker == "model"
    assert res.counts.get("held_model_mismatch") == 1


def test_cli_provider_transient_terminal_does_not_block_next_camera():
    sb, jobs = _two_camera_sb()

    def analyzer(frame_sets, model):
        if "c0" in frame_sets:
            raise CliBatchError("provider_error: timeout", disposition="retryable")
        return _ok_batch(frame_sets)

    res = process_cli_jobs(sb, jobs, analyzer=analyzer, auth_check=lambda: None, download_fn=lambda _k, d: d, extract_fn=_six)
    assert res.breaker is None
    assert res.counts.get("succeeded") == 1
    j0 = next(r for r in sb.store["clip_vlm_jobs"] if r["id"] == "j0")
    assert j0["status"] in ("failed_retryable", "failed_terminal")


def test_cli_provider_batch_exception_marks_all_ready_with_same_safe_diagnostic():
    jobs = [_cli_job("j0", "c0", "cam", "run", Slot.CUSTOMER_HIGHLIGHT.value),
            _cli_job("j1", "c1", "cam", "run", Slot.SUBTLE_BEHAVIOR.value)]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [{"id": "c0", "r2_key": "c0.mp4"}, {"id": "c1", "r2_key": "c1.mp4"}]})
    diag = clip_failure_diagnostic(ValueError("bad envelope at /Users/secret/x.py"), phase="envelope")

    def analyzer(frame_sets, model):
        raise CliBatchError("provider_error: invalid_envelope", disposition="no_retry", diagnostic=diag)

    res = process_cli_jobs(sb, jobs, analyzer=analyzer, auth_check=lambda: None, download_fn=lambda _k, d: d, extract_fn=_six)
    stored = sb.store["clip_vlm_jobs"]
    assert res.breaker is None  # envelope 는 breaker 아님
    payloads = [row["failure_diagnostic"] for row in stored]
    assert payloads[0] == payloads[1] and payloads[0]["phase"] == "envelope"
    assert "/Users/secret" not in json.dumps(payloads[0])


def test_cli_provider_success_after_retry_records_recovered_diagnostic():
    jobs = [_cli_job("j0", "c0", "cam", "run", Slot.CUSTOMER_HIGHLIGHT.value)]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [{"id": "c0", "r2_key": "c0.mp4"}]})
    diag = clip_failure_diagnostic(TimeoutError("t"), phase="process")
    calls = []

    def analyzer(frame_sets, model):
        calls.append(1)
        if len(calls) == 1:
            raise CliBatchError("provider_error: timeout", disposition="retryable", diagnostic=diag)
        return _ok_batch(frame_sets)

    res = process_cli_jobs(sb, jobs, analyzer=analyzer, auth_check=lambda: None, download_fn=lambda _k, d: d, extract_fn=_six)
    assert res.counts.get("succeeded") == 1
    j0 = sb.store["clip_vlm_jobs"][0]
    assert j0["error_code"] is None
    assert j0["failure_diagnostic"]["recovered"] is True
    assert j0["attempt_count"] == 1  # subretry 가 durable attempt 를 늘리지 않음


# --- Task 8: end-to-end scenarios through run() (fake SB/R2/frames/Claude/Slack) ---
from datetime import timezone as _tz

_INT_NOW = datetime(2026, 7, 16, 2, tzinfo=ZoneInfo("Asia/Seoul"))  # window UTC [15:00,17:00) 07-15
_CUR_WS = "2026-07-15T15:30:00+00:00"
_OLD_WS = "2026-07-15T13:00:00+00:00"


def _seed_job(job_id, clip_id, camera, slot, *, selector="budget-router-v1", status="queued", window_start=_CUR_WS, attempt=0):
    # 하나의 selector run = camera+window (production 과 동일하게 batch 를 묶는다)
    return {"id": job_id, "clip_id": clip_id, "camera_id": camera, "selector_run_id": f"{camera}-{window_start}",
            "slot": slot, "selector_version": selector, "window_start": window_start, "window_end": "2026-07-15T17:00:00+00:00",
            "status": status, "attempt_count": attempt, "queued_at": window_start, "created_at": window_start,
            "cost_usd": "0", "model_actual": None, "result": None, "failure_diagnostic": None, "error_code": None}


def _seed_clip(clip_id):
    return {"id": clip_id, "r2_key": f"{clip_id}.mp4", "started_at": "2026-07-01T00:00:00+00:00"}  # window 밖 → 신규 selection 0


def _int_run(sb, *, analyzer, auth_check=lambda: None, hostname="mac-mini", acquire_lock_fn=lambda: object()):
    sent = []
    calls = {"analyze": 0}

    def recording(frame_sets, model):
        calls["analyze"] += 1
        return analyzer(frame_sets, model)

    def process_fn(s, jobs):
        return process_cli_jobs(s, jobs, analyzer=recording, auth_check=auth_check,
                                download_fn=lambda _k, d: d, extract_fn=_six)

    rc = run(sb=sb, now=_INT_NOW, enabled=True, process_fn=process_fn,
             expected_host="mac-mini", hostname_fn=lambda: hostname,
             acquire_lock_fn=acquire_lock_fn, release_lock_fn=lambda _l: None,
             send_fn=lambda t: sent.append(t) or True)
    return rc, sent, calls


def _ok(frame_sets, model="claude-sonnet-5", action="moving", mismatch=None):
    results = {cid: {"clip_id": cid, "action": action, "confidence": .8, "reasoning": "secret /Users/x"} for cid in frame_sets}
    return CliBatchResult("s", model, mismatch or model, results, Usage(8, 0, 0, 2), .0, mismatch is not None)


def _no_forbidden_writes(sb):
    assert not ({"behavior_logs", "behavior_labels", "camera_clips"} & set(sb.store))


def test_scenario_a_normal_four_succeed():
    jobs = [_seed_job(f"j{i}", f"c{i}", "camA", list(Slot)[i].value) for i in range(4)]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [_seed_clip(f"c{i}") for i in range(4)]})
    rc, sent, calls = _int_run(sb, analyzer=_ok)
    assert rc == 0 and calls["analyze"] == 1
    assert all(r["status"] == "succeeded" for r in sb.store["clip_vlm_jobs"])
    assert len(sent) == 1 and "성공 4" in sent[0]
    assert "/Users/x" not in sent[0]  # reasoning 미노출
    _no_forbidden_writes(sb)


def test_scenario_b_current_then_recovery_backfill_untouched():
    cur = [_seed_job(f"j{i}", f"c{i}", "camA", list(Slot)[i].value) for i in range(4)]
    old = [_seed_job(f"o{i}", f"oc{i}", "camA", list(Slot)[i].value, status="failed_retryable", window_start=_OLD_WS) for i in range(4)]
    bf = [_seed_job(f"b{i}", f"bc{i}", "camA", list(Slot)[i].value, selector="budget-router-backfill-20260707-14-v1") for i in range(4)]
    clips = [_seed_clip(cid) for cid in [f"c{i}" for i in range(4)] + [f"oc{i}" for i in range(4)] + [f"bc{i}" for i in range(4)]]
    sb = FakeSB({"clip_vlm_jobs": cur + old + bf, "motion_clips": clips})
    rc, sent, calls = _int_run(sb, analyzer=_ok)
    assert rc == 0 and calls["analyze"] == 2  # current batch + recovery batch
    by_id = {r["id"]: r for r in sb.store["clip_vlm_jobs"]}
    assert all(by_id[f"j{i}"]["status"] == "succeeded" for i in range(4))
    assert all(by_id[f"o{i}"]["status"] == "succeeded" for i in range(4))
    assert all(by_id[f"b{i}"]["status"] == "queued" for i in range(4))  # backfill 0 소비


def test_scenario_c_auth_failure_stops_and_reports():
    jobs = [_seed_job(f"j{i}", f"c{i}", "camA", list(Slot)[i].value) for i in range(4)]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [_seed_clip(f"c{i}") for i in range(4)]})

    def auth_fail():
        raise CliBatchError("not_logged_in", disposition="breaker",
                            diagnostic=clip_failure_diagnostic(RuntimeError("x"), phase="auth"))

    def must_not(*_a, **_k):
        raise AssertionError("analyzer must not run on auth failure")

    rc, sent, calls = _int_run(sb, analyzer=must_not, auth_check=auth_fail)
    assert calls["analyze"] == 0
    assert all(r["error_code"] == "not_logged_in" for r in sb.store["clip_vlm_jobs"])
    assert len(sent) == 1


def test_scenario_d_transient_rc1_recovers():
    jobs = [_seed_job("j0", "c0", "camA", Slot.CUSTOMER_HIGHLIGHT.value)]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [_seed_clip("c0")]})
    state = {"n": 0}

    def flaky(frame_sets, model):
        state["n"] += 1
        if state["n"] == 1:
            raise CliBatchError("provider_error: cli_rc_1", disposition="retryable",
                                diagnostic=clip_failure_diagnostic(RuntimeError("blip"), phase="process"))
        return _ok(frame_sets)

    rc, sent, calls = _int_run(sb, analyzer=flaky)
    j0 = sb.store["clip_vlm_jobs"][0]
    assert calls["analyze"] == 2 and j0["status"] == "succeeded"
    assert j0["failure_diagnostic"]["recovered"] is True
    assert j0["attempt_count"] == 1 and len(sent) == 1  # durable reservation 1회


def test_scenario_e_model_mismatch_holds_and_stops():
    jobs = [_seed_job("j0", "c0", "camA", Slot.CUSTOMER_HIGHLIGHT.value),
            _seed_job("j1", "c1", "camB", Slot.CUSTOMER_HIGHLIGHT.value)]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [_seed_clip("c0"), _seed_clip("c1")]})
    rc, sent, calls = _int_run(sb, analyzer=lambda fs, m: _ok(fs, mismatch="claude-sonnet-4-6"))
    assert calls["analyze"] == 1  # 2번째 camera batch 중단
    by_id = {r["id"]: r for r in sb.store["clip_vlm_jobs"]}
    assert by_id["j0"]["status"] == "held_model_mismatch"
    assert by_id["j1"]["status"] == "queued"
    assert len(sent) == 1 and "모델불일치" in sent[0]


def test_scenario_f_slack_failure_does_not_change_db_or_recall_claude():
    jobs = [_seed_job(f"j{i}", f"c{i}", "camA", list(Slot)[i].value) for i in range(4)]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [_seed_clip(f"c{i}") for i in range(4)]})
    calls = {"analyze": 0}

    def recording(fs, m):
        calls["analyze"] += 1
        return _ok(fs)

    def process_fn(s, j):
        return process_cli_jobs(s, j, analyzer=recording, auth_check=lambda: None, download_fn=lambda _k, d: d, extract_fn=_six)

    def failing_send(_t):
        raise RuntimeError("slack down")

    rc = run(sb=sb, now=_INT_NOW, enabled=True, process_fn=process_fn, expected_host="mac-mini",
             hostname_fn=lambda: "mac-mini", acquire_lock_fn=lambda: object(), release_lock_fn=lambda _l: None,
             send_fn=failing_send)
    assert rc == 0 and calls["analyze"] == 1
    assert all(r["status"] == "succeeded" for r in sb.store["clip_vlm_jobs"])


def test_scenario_g_lock_loser_reports_blocked_and_stops():
    jobs = [_seed_job("j0", "c0", "camA", Slot.CUSTOMER_HIGHLIGHT.value)]
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [_seed_clip("c0")]})

    def must_not(*_a, **_k):
        raise AssertionError("lock loser must not analyze")

    rc, sent, calls = _int_run(sb, analyzer=must_not, acquire_lock_fn=lambda: None)
    assert rc != 0 and calls["analyze"] == 0
    assert sb.store["clip_vlm_jobs"][0]["status"] == "queued"
    assert len(sent) == 1 and "blocked_lock" in sent[0]


def test_scenario_h_no_candidates_reports_zero():
    sb = FakeSB({})
    rc, sent, calls = _int_run(sb, analyzer=lambda *_a: (_ for _ in ()).throw(AssertionError("no claude")))
    assert rc == 0 and calls["analyze"] == 0
    assert len(sent) == 1 and "후보 0개" in sent[0]
    _no_forbidden_writes(sb)
