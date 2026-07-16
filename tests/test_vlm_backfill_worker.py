from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from reporter.vlm_backfill_gate import GateEnrichment
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION, EPOCH_START, bucket_plans, source_nights
from reporter.vlm_backfill_worker import (
    InsufficientCandidates,
    backfill_allowed_now,
    choose_target_camera,
    next_rolling_source_date,
    next_source_date,
    prepare_wave,
    run,
)

_KST = ZoneInfo("Asia/Seoul")
from reporter.vlm_models import CandidateClip, SelectedCandidate


def _run(**kwargs):
    """host guard 를 통과시키는 기본 host 를 주입한 run() 래퍼(H2 이후 공통)."""
    kwargs.setdefault("expected_host", "test-host")
    kwargs.setdefault("hostname_fn", lambda: "test-host")
    return run(**kwargs)
from tests._fakes import FakeSB


def _motion_clip(cid: str, camera: str, started_at: datetime) -> dict:
    return {
        "id": cid,
        "camera_id": camera,
        "started_at": started_at.isoformat(),
        "duration_sec": 30,
        "r2_key": f"clips/{cid}.mp4",
        "motion_score": 1,
        "width": 1280,
        "height": 720,
    }


def _candidate(cid: str, started_at: datetime) -> CandidateClip:
    return CandidateClip(cid, "camera-a", started_at, 30, f"clips/{cid}.mp4", 1, 1280, 720)


def _load_wave(_sb, start, _end, _policy, _selector):
    return [_candidate(f"{start.hour:02d}-{index}", start + timedelta(minutes=index * 5)) for index in range(8)]


def _enrich(clips, **_kwargs):
    return GateEnrichment(clips, {clip.id: {"source": "fake"} for clip in clips}, {"reused": 0, "assessed": len(clips), "failed": 0})


def _select(clips, plan, _history):
    return [
        SelectedCandidate(clips[index], slot, f"episode-{plan.bucket_index}-{index}", {}, slot.value)
        for index, slot in enumerate(plan.required_slots)
    ]


def _jobs_for_day(day, *, count: int, status: str = "succeeded", completed_at: datetime | None = None):
    plans = bucket_plans(day)
    rows = []
    for index in range(count):
        plan = plans[index % 8]
        rows.append({
            "id": f"job-{day}-{index}",
            "clip_id": f"clip-{day}-{index}",
            "camera_id": "camera-a",
            "selector_version": BACKFILL_SELECTOR_VERSION,
            "window_start": plan.start.isoformat(),
            "window_end": plan.end.isoformat(),
            "slot": plan.required_slots[index % len(plan.required_slots)].value,
            "status": status,
            "rank_features": {"source_date": day.isoformat(), "bucket_index": plan.bucket_index},
            "attempt_count": 1,
            "error_code": None,
            "queued_at": datetime(2026, 7, 15, 0, tzinfo=timezone.utc).isoformat(),
            "completed_at": (completed_at or datetime(2026, 7, 15, tzinfo=timezone.utc)).isoformat(),
        })
    return rows


def test_backfill_allowed_only_between_07_and_20_kst():
    for hour, minute in [(7, 0), (12, 0), (19, 59)]:
        assert backfill_allowed_now(datetime(2026, 7, 15, hour, minute, tzinfo=_KST)) is True
    for hour, minute in [(20, 0), (22, 0), (0, 0), (4, 0), (6, 59)]:
        assert backfill_allowed_now(datetime(2026, 7, 15, hour, minute, tzinfo=_KST)) is False


def test_backfill_allowed_converts_utc_to_kst():
    assert backfill_allowed_now(datetime(2026, 7, 15, 3, tzinfo=timezone.utc)) is True   # KST 12:00
    assert backfill_allowed_now(datetime(2026, 7, 15, 13, tzinfo=timezone.utc)) is False  # KST 22:00


def test_backfill_night_run_is_noop_before_lock_or_db():
    def boom(*_args, **_kwargs):
        raise AssertionError("night backfill must no-op before lock/DB/Gate/Claude")
    assert _run(
        now=datetime(2026, 7, 15, 22, tzinfo=_KST),
        acquire_vlm_lock_fn=boom, release_vlm_lock_fn=boom,
        acquire_activity_lock_fn=boom, process_fn=boom, prepare_fn=boom,
    ) == 0


def test_backfill_noop_cycles_send_no_slack():
    sent = []
    # 야간 guard no-op → Slack 0
    assert _run(now=datetime(2026, 7, 15, 22, tzinfo=_KST), acquire_vlm_lock_fn=lambda: object(),
               release_vlm_lock_fn=lambda _fd: None, send_fn=lambda t: sent.append(t) or True) == 0
    # activity worker busy defer → Slack 0
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)]})
    assert _run(sb=sb, now=datetime(2026, 7, 15, 11, tzinfo=_KST),
               acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
               acquire_activity_lock_fn=lambda: None,
               prepare_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("defer")),
               send_fn=lambda t: sent.append(t) or True) == 0
    assert sent == []


def test_target_camera_is_largest_without_hardcoded_uuid():
    start = bucket_plans(source_nights()[0])[0].start
    rows = [_motion_clip(f"a-{i}", "camera-a", start + timedelta(minutes=i)) for i in range(5)]
    rows += [_motion_clip("b-0", "camera-b", start) ]
    assert choose_target_camera(FakeSB({"motion_clips": rows}), source_nights()) == "camera-a"


def test_prepare_wave_zero_candidates_writes_nothing():
    # rolling: 0 후보면 raise 대신 빈 wave 반환, 아무 것도 생성하지 않음(worker 가 no_candidates 처리)
    sb = FakeSB()
    wave = prepare_wave(
        sb, EPOCH_START, "camera-a", load_fn=_load_wave, enrich_fn=_enrich,
        select_fn=lambda *_args: [], history_fn=lambda *_args: {},
    )
    assert len(wave.selected) == 0
    assert sb.store.get("clip_vlm_selector_runs", []) == []
    assert sb.store.get("clip_vlm_jobs", []) == []


def test_prepare_wave_insufficient_creates_only_available_and_dedups():
    # 1~29 후보: 존재분만 생성. exclude_clip_ids 로 cross-selector 중복 제외.
    sb = FakeSB()
    excluded = {"20-0"}  # bucket0(20시) 첫 clip 은 이미 다른 job 존재 → 제외
    wave = prepare_wave(
        sb, EPOCH_START, "camera-a", load_fn=_load_wave, enrich_fn=_enrich, select_fn=_select,
        history_fn=lambda *_a: {}, exclude_clip_ids=excluded, max_new=5)
    assert 0 < len(wave.selected) <= 5           # max_new clamp
    ids = [item.clip.id for item in wave.selected]
    assert len(ids) == len(set(ids))             # wave 내 unique
    assert "20-0" not in ids                     # 제외 clip 미포함


def test_prepare_wave_creates_8_runs_and_30_jobs_idempotently():
    sb = FakeSB()
    kwargs = dict(load_fn=_load_wave, enrich_fn=_enrich, select_fn=_select, history_fn=lambda *_args: {})
    first = prepare_wave(sb, source_nights()[0], "camera-a", **kwargs)
    second = prepare_wave(sb, source_nights()[0], "camera-a", **kwargs)
    assert len(first.selected) == len(second.selected) == 30
    assert len(sb.store["clip_vlm_selector_runs"]) == 8
    assert len(sb.store["clip_vlm_jobs"]) == 30
    assert all(job["reserved_cost_usd"] == "0" for job in sb.store["clip_vlm_jobs"])
    assert all(job["rank_features"]["source_date"] == "2026-07-07" for job in sb.store["clip_vlm_jobs"])
    assert {job["rank_features"]["bucket_index"] for job in sb.store["clip_vlm_jobs"]} == set(range(8))
    assert all(job["rank_features"]["backfill_version"] == BACKFILL_SELECTOR_VERSION for job in sb.store["clip_vlm_jobs"])
    assert all(job["rank_features"]["gate_snapshot"]["source"] == "fake" for job in sb.store["clip_vlm_jobs"])


def test_completed_wave_advances_and_partial_wave_resumes_same_date():
    days = source_nights()
    complete = FakeSB({"clip_vlm_jobs": _jobs_for_day(days[0], count=30, completed_at=datetime(2026, 7, 15, 0, tzinfo=timezone.utc))})
    assert next_source_date(complete, "camera-a", datetime(2026, 7, 15, 2, tzinfo=timezone.utc)) == days[1]
    partial = FakeSB({"clip_vlm_jobs": _jobs_for_day(days[0], count=29)})
    assert next_source_date(partial, "camera-a", datetime(2026, 7, 15, 2, tzinfo=timezone.utc)) == days[0]


def test_next_wave_waits_55_minutes_after_manual_canary():
    completed = datetime(2026, 7, 15, 0, tzinfo=timezone.utc)
    sb = FakeSB({"clip_vlm_jobs": _jobs_for_day(source_nights()[0], count=30, completed_at=completed)})
    assert next_source_date(sb, "camera-a", completed + timedelta(minutes=54)) is None
    assert next_source_date(sb, "camera-a", completed + timedelta(minutes=55)) == source_nights()[1]


def test_backfill_never_drains_regular_selector_jobs():
    # 새 계약(§7.2): backfill worker 는 정규 selector job 을 절대 소비하지 않는다.
    # (구계약은 regular-first drain 이었고 이 테스트가 그 회귀를 막는다.)
    start = bucket_plans(source_nights()[0])[0].start
    regular = {"id": "regular", "selector_version": "budget-router-v1", "status": "queued", "queued_at": "2026-07-15T00:00:00+00:00"}
    sb = FakeSB({"clip_vlm_jobs": [regular], "motion_clips": [_motion_clip("a", "camera-a", start)]})
    calls = []
    assert _run(
        sb=sb,
        now=datetime(2026, 7, 15, 2, tzinfo=timezone.utc),
        process_fn=lambda _sb, jobs, **_k: calls.append([job["id"] for job in jobs]) or {"succeeded": len(jobs)},
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        acquire_activity_lock_fn=lambda: None,  # prepare 직전 defer — regular 이 새지 않는지만 검증
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not build wave")),
    ) == 0
    assert calls == []  # 정규 job 은 backfill process_fn 에 넘어가지 않음
    assert sb.store["clip_vlm_jobs"][0]["status"] == "queued"  # 정규 queue 불변


def test_activity_worker_lock_defers_gate_prepool():
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)]})
    assert _run(
        sb=sb,
        now=datetime(2026, 7, 15, 2, tzinfo=timezone.utc),
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        acquire_activity_lock_fn=lambda: None,
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must defer")),
    ) == 0


def test_previous_quota_error_blocks_future_backfill_wave():
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({
        "motion_clips": [_motion_clip("a", "camera-a", start)],
        "clip_vlm_jobs": [{
            "id": "limited",
            "camera_id": "camera-a",
            "selector_version": BACKFILL_SELECTOR_VERSION,
            "status": "failed_retryable",
            "error_code": "quota_exceeded",
        }],
    })
    assert _run(
        sb=sb,
        now=datetime(2026, 7, 15, 11, tzinfo=_KST),
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must remain blocked")),
    ) == 0


def test_existing_30_job_wave_resumes_without_gate_or_reselection():
    jobs = _jobs_for_day(source_nights()[0], count=30, status="failed_retryable")
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)], "clip_vlm_jobs": jobs})
    calls = []
    sent = []
    assert _run(
        sb=sb,
        now=datetime(2026, 7, 15, 11, tzinfo=_KST),
        process_fn=lambda _sb, due, **_k: calls.append([job["id"] for job in due]) or {"succeeded": len(due)},
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        prepare_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("resume must not reselect")),
        acquire_activity_lock_fn=lambda: (_ for _ in ()).throw(AssertionError("resume must not run Gate")),
        send_fn=lambda text: sent.append(text) or True,
    ) == 0
    assert len(calls) == 1
    assert len(calls[0]) == 30
    assert len(sent) == 1 and "과거 영상 VLM 분석" in sent[0]  # 실제 처리 cycle → 진행률 1회


def test_rolling_resumes_open_night_without_reselection_or_recreate():
    # 부족(29) night 라도 open 이면 resume 처리. 재-wave/재생성 없음.
    jobs = _jobs_for_day(source_nights()[0], count=29, status="failed_retryable")
    start = bucket_plans(source_nights()[0])[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)], "clip_vlm_jobs": jobs})
    calls = []; sent = []
    assert _run(
        sb=sb,
        now=datetime(2026, 7, 15, 11, tzinfo=_KST),
        process_fn=lambda _sb, due, **_k: calls.append(len(due)) or {"succeeded": len(due)},
        acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
        prepare_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("resume must not reselect")),
        acquire_activity_lock_fn=lambda: (_ for _ in ()).throw(AssertionError("resume must not run Gate")),
        send_fn=lambda t: sent.append(t) or True,
    ) == 0
    assert calls == [29]                          # 29 due 재개
    assert len(sb.store["clip_vlm_jobs"]) == 29    # 재생성 없음
    assert len(sent) == 1


def test_rolling_daily_cap_blocks_new_wave_at_600():
    # 오늘 이미 600개 생성 → 신규 wave 생성 안 함(no-op), Slack 0
    today_jobs = [{"id": f"cap-{i:04d}", "selector_version": BACKFILL_SELECTOR_VERSION, "status": "succeeded",
                   "created_at": "2026-07-15T02:00:00+00:00",  # KST 11:00 오늘
                   "rank_features": {"source_date": "2026-07-07"}} for i in range(600)]
    # 07-07 은 전부 succeeded(open 0) → 파생 complete. 신규 대상은 07-08(미생성).
    start = bucket_plans(EPOCH_START + timedelta(days=1))[0].start  # 07-08 night
    sb = FakeSB({"clip_vlm_jobs": today_jobs, "motion_clips": [_motion_clip("a", "camera-a", start)]})
    sent = []
    assert _run(sb=sb, now=datetime(2026, 7, 15, 2, tzinfo=timezone.utc),  # KST 11:00
               acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
               prepare_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("cap must block new wave")),
               send_fn=lambda t: sent.append(t) or True) == 0
    assert sent == []


def test_rolling_no_candidates_night_closes_via_ledger_no_slack():
    # 대상 night 에 motion clip 0 → ledger no_candidates, Slack 0
    sb = FakeSB()  # motion_clips 없음
    sent = []
    assert _run(sb=sb, now=datetime(2026, 7, 15, 2, tzinfo=timezone.utc),  # KST 11:00
               acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
               prepare_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no wave for 0 clips")),
               send_fn=lambda t: sent.append(t) or True) == 0
    ledger = sb.store.get("vlm_backfill_ledger", [])
    assert any(r["status"] == "no_candidates" for r in ledger)
    assert sent == []


def test_backfill_host_mismatch_fails_closed_before_any_work():
    # H2: MacBook 등 expected host 불일치 → lock/DB/R2/Gate/Claude/Slack 전 fail-closed(nonzero)
    def boom(*_a, **_k):
        raise AssertionError("host mismatch must stop before lock/DB/Claude/Slack")
    rc = run(now=datetime(2026, 7, 15, 11, tzinfo=_KST),
             expected_host="mac-mini.verified", hostname_fn=lambda: "macbook.local",
             acquire_vlm_lock_fn=boom, allowed_fn=boom, process_fn=boom, prepare_fn=boom, send_fn=boom)
    assert rc != 0


def test_backfill_blank_expected_host_fails_closed():
    def boom(*_a, **_k):
        raise AssertionError("blank expected host must fail closed")
    rc = run(now=datetime(2026, 7, 15, 11, tzinfo=_KST),
             expected_host="", hostname_fn=lambda: "macbook.local",
             acquire_vlm_lock_fn=boom, allowed_fn=boom, send_fn=boom)
    assert rc != 0


def _ledger_row(source_date, scope="camera-a", status="processing"):
    return {"id": "l0", "selector_version": BACKFILL_SELECTOR_VERSION,
            "source_date": source_date.isoformat(), "scope": scope, "status": status}


def test_rolling_claim_loser_no_ops_without_gate_or_creation():
    # H1.1: 이미 claim 된 날짜 → claim=false → Gate/R2/Claude/job 생성 0, no-op
    start = bucket_plans(EPOCH_START)[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)],
                 "vlm_backfill_ledger": [_ledger_row(EPOCH_START)]})

    def boom(*_a, **_k):
        raise AssertionError("claim loser must not build/process")

    assert _run(sb=sb, now=datetime(2026, 7, 15, 11, tzinfo=_KST),
                acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
                acquire_activity_lock_fn=lambda: object(), release_activity_lock_fn=lambda _l: None,
                prepare_fn=boom, process_fn=boom, send_fn=lambda t: True) == 0


def test_rolling_activity_lock_defer_writes_no_claim():
    # H1.2: activity lock 을 먼저 확보 → lock 실패 시 claim(processing ledger) 을 남기지 않는다
    start = bucket_plans(EPOCH_START)[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)]})
    assert _run(sb=sb, now=datetime(2026, 7, 15, 11, tzinfo=_KST),
                acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
                acquire_activity_lock_fn=lambda: None,
                prepare_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no wave")),
                send_fn=lambda t: True) == 0
    assert sb.store.get("vlm_backfill_ledger", []) == []  # claim 미기록


def test_rolling_pre_create_exception_releases_claim_for_retry():
    # H1.3: job 생성 전 예외 → claim 해제(job 없으므로) → 날짜 고착 없음
    start = bucket_plans(EPOCH_START)[0].start
    sb = FakeSB({"motion_clips": [_motion_clip("a", "camera-a", start)]})

    def prepare_boom(*_a, **_k):
        raise RuntimeError("gate down")

    assert _run(sb=sb, now=datetime(2026, 7, 15, 11, tzinfo=_KST),
                acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
                acquire_activity_lock_fn=lambda: object(), release_activity_lock_fn=lambda _l: None,
                prepare_fn=prepare_boom, send_fn=lambda t: True) == 0
    assert not any(r["source_date"] == EPOCH_START.isoformat()
                   for r in sb.store.get("vlm_backfill_ledger", []))  # 해제됨 → 재시도 가능


def test_rolling_partial_create_keeps_claim_and_resumes():
    # H1.4/H1.6: job 이 이미 있으면 release 거부(DB 강제), 다음 cycle 은 resume
    jobs = _jobs_for_day(EPOCH_START, count=3, status="failed_retryable")
    start = bucket_plans(EPOCH_START)[0].start
    sb = FakeSB({"clip_vlm_jobs": jobs, "motion_clips": [_motion_clip("a", "camera-a", start)],
                 "vlm_backfill_ledger": [_ledger_row(EPOCH_START)]})
    from reporter.vlm_store import release_backfill_claim
    assert release_backfill_claim(sb, BACKFILL_SELECTOR_VERSION, EPOCH_START, "camera-a") is False  # job 있으면 해제 금지
    calls = []
    assert _run(sb=sb, now=datetime(2026, 7, 15, 11, tzinfo=_KST),
                process_fn=lambda _sb, due, **_k: calls.append(len(due)) or {"succeeded": len(due)},
                acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
                prepare_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("resume must not reselect")),
                acquire_activity_lock_fn=lambda: (_ for _ in ()).throw(AssertionError("resume must not run Gate")),
                send_fn=lambda t: True) == 0
    assert calls == [3]  # partial 은 resume 으로 복구


def test_rolling_passes_regular_vlm_deadline_to_process():
    # H4: backfill 이 process 에 정규 VLM 시각 deadline 을 전달(정규까지 lock 미보유 보장)
    from reporter.vlm_rolling import next_regular_vlm
    now = datetime(2026, 7, 16, 22, 35, tzinfo=_KST)  # 허용(22:35), 다음 정규 = 07-17 00:00
    jobs = _jobs_for_day(source_nights()[0], count=5, status="failed_retryable")
    sb = FakeSB({"clip_vlm_jobs": jobs,
                 "motion_clips": [_motion_clip("a", "camera-a", bucket_plans(source_nights()[0])[0].start)]})
    captured = {}

    def process_fn(_sb, due, **kw):
        captured.update(kw); return {"succeeded": len(due)}

    assert _run(sb=sb, now=now, process_fn=process_fn,
                acquire_vlm_lock_fn=lambda: object(), release_vlm_lock_fn=lambda _fd: None,
                send_fn=lambda t: True) == 0
    assert captured.get("deadline") == next_regular_vlm(now)


def test_next_regular_vlm_is_next_of_22_00_02_04():
    from reporter.vlm_rolling import next_regular_vlm
    assert next_regular_vlm(datetime(2026, 7, 16, 22, 35, tzinfo=_KST)) == datetime(2026, 7, 17, 0, tzinfo=_KST).astimezone(timezone.utc)
    assert next_regular_vlm(datetime(2026, 7, 16, 12, 0, tzinfo=_KST)) == datetime(2026, 7, 16, 22, tzinfo=_KST).astimezone(timezone.utc)
    assert next_regular_vlm(datetime(2026, 7, 16, 0, 30, tzinfo=_KST)) == datetime(2026, 7, 16, 2, tzinfo=_KST).astimezone(timezone.utc)


def test_rolling_schedule_guard_noop_before_lock():
    def boom(*_a, **_k):
        raise AssertionError("schedule guard must no-op before lock/DB")
    # 21:35 KST = 정규 22:00 25분 전 → guard skip
    assert _run(now=datetime(2026, 7, 15, 21, 35, tzinfo=_KST),
               acquire_vlm_lock_fn=boom, process_fn=boom, prepare_fn=boom, send_fn=boom) == 0
