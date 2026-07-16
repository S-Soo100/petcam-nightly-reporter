# Historical VLM Backfill 240 Implementation Plan

> **SUPERSEDED (2026-07-16)** — 고정 8박 240개는 rolling backfill 로 대체됨: `specs/2026-07-16-rolling-vlm-backfill-design.md`. 이 문서는 history 로 보존.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **2026-07-16 단일 호스트 운영 하드닝 반영** (`docs/superpowers/plans/2026-07-16-vlm-single-host-operations-hardening.md`):
> - backfill worker 의 **정규 selector-first drain 은 제거**됐다. backfill worker 는 `BACKFILL_SELECTOR_VERSION` job 만 생성·조회·처리한다(정규 queue 교차 소비 금지).
> - backfill 은 정규 야간 schedule·shared Claude lock 과 겹치지 않게 **07:00~19:59 KST 에만** 실행한다(`backfill_allowed_now`). installer 도 상시/주기 트리거 대신 **07~19시 정각 calendar** 만 만든다.
> - backfill LaunchAgent 는 진행률 완료가 확인되기 전까지 유지하며, 완료 확인 전 삭제·job 폐기를 하지 않는다.

**Goal:** 2026-07-07~07-14 source night에서 주 카메라 영상 30개씩 총 240개를 local Gate 후보 보강 후 Claude Sonnet 5로 오늘 한 시간 간격으로 shadow 분석한다.

**Architecture:** `vlm_backfill_selector.py`가 날짜·시간 bucket·prepool·슬롯 quota를 순수하게 계산하고, `vlm_backfill_gate.py`가 기존 evidence를 재사용하거나 Gate를 메모리에서만 실행한다. `vlm_backfill_worker.py`는 기존 selector run/job RPC와 Claude CLI batch runtime을 재사용하며, 별도 LaunchAgent가 시간당 미완료 source night 하나를 처리한다. 새 DB migration은 없다.

**Tech Stack:** Python 3.12, pytest, Supabase/PostgREST, gecko-vision-gate, OpenCV, Claude Code CLI, macOS launchd.

## Global Constraints

- source nights는 2026-07-07~2026-07-14이고 각 night는 20:00~익일 04:00 KST다.
- 대상은 전체 기간 valid clip 수가 가장 많은 주 카메라 한 대이며 UUID를 하드코딩하지 않는다.
- night당 30개, 총 240개다. 8개 시간 bucket 각각은 전체 기간에 정확히 30개가 된다.
- slot quota는 night당 `customer_highlight=8`, `subtle_behavior=8`, `diversity_discovery=7`, `exclusion_audit=7`이다.
- local Gate는 후보 evidence용이며 hard skip·GT·activity DB write에 사용하지 않는다.
- provider는 `claude_cli_batch`, exact model은 `claude-sonnet-5`, clip당 6 frames, batch당 최대 4 clips다.
- VLM 결과는 `clip_vlm_selector_runs`·`clip_vlm_jobs`에만 저장한다. 앱·GT·하이라이트·활동시간은 바꾸지 않는다.
- 정규 VLM worker와 같은 file lock을 사용한다. 인증·quota·model mismatch·clip set mismatch면 이후 wave를 안전 중단한다.
- actual API cost는 `$0`; API equivalent만 provenance/report에 기록한다.
- 구현 중 DB write·LaunchAgent 설치는 production canary task 전까지 금지한다.

---

### Task 1: 날짜·시간·슬롯 quota와 deterministic prepool

**Files:**
- Create: `reporter/vlm_backfill_selector.py`
- Create: `tests/test_vlm_backfill_selector.py`
- Modify: `specs/2026-07-15-historical-vlm-backfill-design.md`

**Interfaces:**
- Produces: `source_nights() -> tuple[date, ...]`
- Produces: `bucket_plans(source_date: date) -> tuple[BucketPlan, ...]`
- Produces: `build_prepool(clips: list[CandidateClip], limit: int = 15) -> list[CandidateClip]`
- Produces: `select_bucket_candidates(clips, plan, history) -> list[SelectedCandidate]`

- [ ] **Step 1: quota·determinism·중복 억제 실패 테스트를 작성한다**

```python
def test_eight_nights_are_240_and_each_hour_is_30():
    plans = [p for day in source_nights() for p in bucket_plans(day)]
    assert len(plans) == 64
    assert sum(len(p.required_slots) for p in plans) == 240
    assert [sum(len(p.required_slots) for p in plans if p.bucket_index == b) for b in range(8)] == [30] * 8

def test_each_night_has_8_8_7_7_slots():
    for day in source_nights():
        counts = Counter(slot for p in bucket_plans(day) for slot in p.required_slots)
        assert counts == {
            Slot.CUSTOMER_HIGHLIGHT: 8,
            Slot.SUBTLE_BEHAVIOR: 8,
            Slot.DIVERSITY_DISCOVERY: 7,
            Slot.EXCLUSION_AUDIT: 7,
        }

def test_prepool_is_deterministic_and_at_most_15():
    clips = make_clips(80)
    assert build_prepool(clips) == build_prepool(list(reversed(clips)))
    assert 4 <= len(build_prepool(clips)) <= 15

def test_missing_natural_slot_uses_annotated_fallback():
    selected = select_bucket_candidates(no_gate_evidence_clips(15), four_slot_plan(), {})
    assert len(selected) == 4
    assert len({x.clip.id for x in selected}) == 4
    assert any("fallback" in x.selection_reason for x in selected)
```

- [ ] **Step 2: 대상 테스트가 import 실패로 RED인지 확인한다**

Run: `uv run pytest tests/test_vlm_backfill_selector.py -q`  
Expected: FAIL with `ModuleNotFoundError: reporter.vlm_backfill_selector`.

- [ ] **Step 3: 순수 selector 최소 구현을 작성한다**

```python
BACKFILL_SELECTOR_VERSION = "budget-router-backfill-20260707-14-v1"
SOURCE_DATES = tuple(date(2026, 7, day) for day in range(7, 15))

@dataclass(frozen=True, slots=True)
class BucketPlan:
    source_date: date
    bucket_index: int
    start: datetime
    end: datetime
    required_slots: tuple[Slot, ...]

def bucket_plans(source_date: date) -> tuple[BucketPlan, ...]:
    i = SOURCE_DATES.index(source_date)
    night_start = datetime.combine(source_date, time(20), KST)
    plans = []
    for b in range(8):
        slots = list(ORDER)
        if b == i:
            slots.remove(Slot.DIVERSITY_DISCOVERY)
        elif b == (i + 4) % 8:
            slots.remove(Slot.EXCLUSION_AUDIT)
        start = night_start + timedelta(hours=b)
        plans.append(BucketPlan(source_date, b, start, start + timedelta(hours=1), tuple(slots)))
    return tuple(plans)
```

`build_prepool`은 clip을 started_at/id로 정렬하고 motion quartile×30분 episode group을 round-robin해 최대 15개를 반환한다. `select_bucket_candidates`는 기존 `select_candidates` 결과를 먼저 쓰고, 빠진 required slot은 남은 clip에서 highlight=높은 motion, subtle=낮은 nonzero motion, diversity=낮은 history count, audit=exclude/unknown 우선 순으로 deterministic fallback한다. 모든 fallback은 `selection_reason`과 `rank_features.fallback=true`를 남긴다.

- [ ] **Step 4: selector 테스트와 기존 router 회귀를 통과시킨다**

Run: `uv run pytest tests/test_vlm_backfill_selector.py tests/test_vlm_router.py -q`  
Expected: PASS.

- [ ] **Step 5: 설계의 target camera 선택 계약을 명시하고 커밋한다**

```bash
git add reporter/vlm_backfill_selector.py tests/test_vlm_backfill_selector.py specs/2026-07-15-historical-vlm-backfill-design.md
git commit -m "feat: 과거 VLM 백필 시간대와 슬롯 selector"
```

---

### Task 2: DB write 없는 local Gate evidence 보강

**Files:**
- Create: `reporter/vlm_backfill_gate.py`
- Create: `tests/test_vlm_backfill_gate.py`
- Modify: `reporter/activity_worker.py`
- Modify: `tests/test_activity_worker.py`

**Interfaces:**
- Produces: `build_activity_policy(version: str | None = None) -> ActivityPolicy`
- Produces: `acquire_activity_lock()`·`release_activity_lock(fd)` for shared detector exclusion.
- Produces: `enrich_prepool(clips, *, checkpoint, detector_factory, download_fn, assess_fn) -> GateEnrichment`
- `GateEnrichment.clips`는 evidence가 합쳐진 immutable `CandidateClip` 목록이다.
- `GateEnrichment.snapshots`는 selected job `rank_features.gate_snapshot`에 넣을 JSON-safe dict다.

- [ ] **Step 1: 재사용·lazy detector·DB 무접근·임시파일 정리 테스트를 작성한다**

```python
def test_existing_evidence_is_reused_without_download_or_detector():
    result = enrich_prepool([clip_with_evidence()], checkpoint="x", detector_factory=must_not_call,
                            download_fn=must_not_call, assess_fn=must_not_call)
    assert result.stats == {"reused": 1, "assessed": 0, "failed": 0}

def test_missing_evidence_loads_detector_once_and_never_receives_db_client(tmp_path):
    calls = Counter()
    result = enrich_prepool(two_raw_clips(), checkpoint="x",
                            detector_factory=counting_detector(calls),
                            download_fn=fake_download(tmp_path, calls), assess_fn=fake_assess(calls))
    assert calls["detector"] == 1 and calls["assess"] == 2
    assert all("gate_snapshot" not in clip.motion_metrics for clip in result.clips)

def test_temp_mp4_is_removed_after_success_and_failure(tmp_path):
    enrich_prepool(raw_clips_with_one_failure(), checkpoint="x", detector_factory=fake_detector,
                   download_fn=fake_download(tmp_path), assess_fn=fake_assess_with_failure)
    assert list(tmp_path.glob("*.mp4")) == []
```

- [ ] **Step 2: RED를 확인한다**

Run: `uv run pytest tests/test_vlm_backfill_gate.py -q`  
Expected: FAIL because module and public policy builder do not exist.

- [ ] **Step 3: policy preset을 public 함수로 만들고 ephemeral enricher를 구현한다**

```python
def build_activity_policy(version: str | None = None) -> ActivityPolicy:
    selected = version or config.ACTIVITY_POLICY_VERSION
    preset = _POLICY_PRESETS.get(selected)
    if preset is None:
        return ActivityPolicy(version=selected, gate_threshold=config.GATE_THRESHOLD)
    return ActivityPolicy(version=selected, **preset)
```

`vlm_backfill_gate.py`는 `dataclasses.replace`로 `activity_decision`, `gecko_visible`, `visibility_confidence`, `gecko_bbox`, `motion_metrics`를 합친 새 `CandidateClip`을 만든다. `PrelabelResult.to_dict()`, `dataclasses.asdict(MotionMetrics)`, `ActivityAssessment`를 JSON-safe snapshot으로 보존한다. 이 함수의 인자에는 Supabase client나 store callback이 없으므로 `clip_prelabels`·`clip_activity_assessments` write가 구조적으로 불가능해야 한다.

- [ ] **Step 4: Gate·activity 회귀 테스트를 통과시킨다**

Run: `uv run pytest tests/test_vlm_backfill_gate.py tests/test_activity_worker.py -q`  
Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_backfill_gate.py tests/test_vlm_backfill_gate.py reporter/activity_worker.py tests/test_activity_worker.py
git commit -m "feat: VLM 백필용 ephemeral Gate evidence"
```

---

### Task 3: durable wave 준비·재개·정규 worker 우선권

**Files:**
- Create: `reporter/vlm_backfill_worker.py`
- Create: `tests/test_vlm_backfill_worker.py`
- Modify: `reporter/vlm_candidate_worker.py`
- Modify: `reporter/vlm_store.py`
- Modify: `tests/_fakes.py`
- Modify: `tests/test_vlm_worker.py`

**Interfaces:**
- Produces: `prepare_wave(sb, source_date, camera_id, ...) -> WavePlan`
- Produces: `wave_status(sb, source_date, camera_id) -> WaveStatus`
- Produces: `run(*, sb=None, now=None, dry_run=False, ...) -> int`
- `choose_target_camera(sb, source_dates: tuple[date, ...]) -> str`를 사용한다.
- `load_due_jobs_for_selector(sb, selector_version, start: datetime | None, end: datetime | None, limit=64) -> list[dict]`를 사용한다.
- `vlm_candidate_worker.acquire_vlm_lock()`·`release_vlm_lock()`을 정규·백필 worker가 공유한다.
- `activity_worker.acquire_activity_lock()`·`release_activity_lock()`으로 local Gate 동시 실행을 막는다.

- [ ] **Step 1: target camera·30개 원자 준비·resume·정규 우선 테스트를 작성한다**

```python
def test_target_camera_is_largest_without_hardcoded_uuid():
    sb = FakeSB({"motion_clips": camera_counts(a=240, b=20)})
    assert choose_target_camera(sb, SOURCE_DATES) == "camera-a"

def test_prepare_wave_validates_30_before_any_rpc_write():
    sb = FakeSB(raw_store())
    with pytest.raises(InsufficientCandidates):
        prepare_wave(sb, SOURCE_DATES[0], "cam", selector=fewer_than_30)
    assert sb.store.get("clip_vlm_selector_runs", []) == []
    assert sb.store.get("clip_vlm_jobs", []) == []

def test_prepare_wave_creates_8_runs_and_30_jobs_idempotently():
    sb = FakeSB(raw_store())
    prepare_wave(sb, SOURCE_DATES[0], "cam", selector=thirty_candidates)
    prepare_wave(sb, SOURCE_DATES[0], "cam", selector=thirty_candidates)
    assert len(sb.store["clip_vlm_selector_runs"]) == 8
    assert len(sb.store["clip_vlm_jobs"]) == 30

def test_regular_due_jobs_defer_backfill_wave():
    assert run(sb=with_regular_queued_job(), process_fn=spy()) == 0
    assert spy.calls == ["regular"]

def test_completed_wave_advances_and_partial_wave_resumes_same_date():
    assert next_source_date(store_with_succeeded(30)) == SOURCE_DATES[1]
    assert next_source_date(store_with_succeeded(29)) == SOURCE_DATES[0]

def test_next_wave_waits_55_minutes_after_manual_canary():
    assert next_source_date(store_with_recently_completed(30), now=completed_at + timedelta(minutes=54)) is None
    assert next_source_date(store_with_recently_completed(30), now=completed_at + timedelta(minutes=55)) == SOURCE_DATES[1]

def test_activity_worker_lock_defers_gate_prepool():
    assert run(sb=raw_store(), acquire_activity_lock_fn=lambda: None, prepare_fn=must_not_call) == 0
```

- [ ] **Step 2: RED를 확인한다**

Run: `uv run pytest tests/test_vlm_backfill_worker.py -q`  
Expected: FAIL because worker and filtered job loader do not exist.

- [ ] **Step 3: shared lock·selector-filtered loader·wave orchestration을 구현한다**

핵심 run 흐름은 아래 순서를 고정한다.

```python
def run(*, sb=None, dry_run=False, process_fn=process_cli_jobs, ...):
    lock = acquire_vlm_lock()
    if lock is None:
        return 0
    try:
        sb = sb or create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        regular = load_due_jobs_for_selector(sb, config.VLM_SELECTOR_VERSION, None, None)
        if regular:
            process_fn(sb, regular)
            return 0
        target = choose_target_camera(sb, SOURCE_DATES)
        day = next_source_date(sb, target)
        if day is None:
            print("[vlm-backfill] complete — no-op")
            return 0
        blocked = blocking_error_for_wave(sb, day, target)
        if blocked:
            print(f"[vlm-backfill] blocked code={blocked}")
            return 0
        activity_lock = acquire_activity_lock()
        if activity_lock is None:
            print("[vlm-backfill] activity worker busy — defer")
            return 0
        try:
            plan = prepare_wave(sb, day, target, persist=not dry_run)
        finally:
            release_activity_lock(activity_lock)
        if dry_run:
            print(json.dumps(plan.to_dict(), ensure_ascii=False))
            return 0
        due = load_due_jobs_for_selector(sb, BACKFILL_SELECTOR_VERSION, plan.start, plan.end)
        stats = process_fn(sb, due)
        print_wave_summary(plan, stats)
        return 0
    finally:
        release_vlm_lock(lock)
```

`prepare_wave`는 8개 bucket의 prepool/enrichment/selection을 전부 메모리에서 끝내고 총 30개·clip unique·slot unique/run을 assert한 뒤에만 8 RPC를 호출한다. 8개 RPC 전체가 단일 트랜잭션은 아니므로 중간 DB 오류가 나면 재실행이 기존 run/job을 멱등 복구한다. selected candidate의 `rank_features`에 `source_date`, `bucket_index`, `gate_snapshot`, `backfill_version`을 넣는다.

- [ ] **Step 4: worker·기존 정규 worker 회귀를 통과시킨다**

Run: `uv run pytest tests/test_vlm_backfill_worker.py tests/test_vlm_worker.py tests/test_vlm_runtime.py -q`  
Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_backfill_worker.py tests/test_vlm_backfill_worker.py reporter/vlm_candidate_worker.py reporter/vlm_store.py tests/_fakes.py tests/test_vlm_worker.py
git commit -m "feat: 240개 VLM 백필 wave와 재개 worker"
```

---

### Task 4: Claude quota 오류 분류와 wave safety stop

**Files:**
- Modify: `reporter/claude_cli_analyzer.py`
- Modify: `tests/test_claude_cli_analyzer.py`
- Modify: `reporter/vlm_backfill_worker.py`
- Modify: `tests/test_vlm_backfill_worker.py`

**Interfaces:**
- `CliBatchError.code`는 `quota_exceeded`, `not_logged_in`, `clip_set_mismatch`, `max_turns_exceeded`, `provider_error` 중 안전한 코드만 노출한다.
- `blocking_error_for_wave(...)`는 quota/auth/model/clip mismatch가 있으면 이후 날짜 진행을 막는다.

- [ ] **Step 1: quota 문구·비밀값 은닉·persistent block 테스트를 작성한다**

```python
@pytest.mark.parametrize("message", [
    "Session limit reached; resets 1pm",
    "Usage limit reached",
    "Rate limit exceeded for subscription",
])
def test_subscription_limit_maps_to_safe_quota_code(message):
    with pytest.raises(CliBatchError, match="quota_exceeded"):
        analyze_batch(frames(), MODEL, runner=envelope_error(message))

def test_quota_error_does_not_leak_account_text():
    with pytest.raises(CliBatchError) as exc:
        analyze_batch(frames(), MODEL, runner=envelope_error("user@example.com session limit"))
    assert str(exc.value) == "quota_exceeded"

def test_blocked_wave_does_not_prepare_or_process_next_date():
    assert run(sb=store_with_error("quota_exceeded"), prepare_fn=must_not_call) == 0
```

- [ ] **Step 2: RED를 확인한다**

Run: `uv run pytest tests/test_claude_cli_analyzer.py tests/test_vlm_backfill_worker.py -q`  
Expected: quota cases FAIL as generic provider error.

- [ ] **Step 3: 최소 안전 매핑과 blocking set을 구현한다**

```python
_QUOTA_MARKERS = ("session limit", "usage limit", "rate limit", "quota")

def _safe_envelope_error(envelope: dict) -> str:
    text = str(envelope.get("result") or "").lower()
    if "not logged in" in text:
        return "not_logged_in"
    if any(marker in text for marker in _QUOTA_MARKERS):
        return "quota_exceeded"
    return "provider_error"

BLOCKING_CODES = {"not_logged_in", "quota_exceeded", "clip_set_mismatch"}
```

- [ ] **Step 4: analyzer·worker 전체 회귀를 통과시킨다**

Run: `uv run pytest tests/test_claude_cli_analyzer.py tests/test_vlm_backfill_worker.py tests/test_vlm_worker.py -q`  
Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/claude_cli_analyzer.py tests/test_claude_cli_analyzer.py reporter/vlm_backfill_worker.py tests/test_vlm_backfill_worker.py
git commit -m "fix: VLM 백필 구독 한도 안전 중단"
```

---

### Task 5: 시간당 임시 LaunchAgent

**Files:**
- Create: `install-launchd-vlm-backfill.sh`
- Create: `tests/test_install_vlm_backfill.py`
- Modify: `.env.example`

**Interfaces:**
- LaunchAgent label: `com.petcam.vlm-historical-backfill`
- Entry point: `uv run python -m reporter.vlm_backfill_worker`
- Schedule: `RunAtLoad=true`, `StartInterval=3600`
- Environment: `HOME`, `USER`, `LOGNAME`, `PATH`, `VLM_PROVIDER=claude_cli_batch`, `ANTHROPIC_MODEL_EXACT=claude-sonnet-5`, `REGISTER_HIGHLIGHTS=0`.

- [ ] **Step 1: launcher contract 실패 테스트를 작성한다**

```python
def test_backfill_launcher_is_hourly_shadow_and_keychain_safe():
    text = Path("install-launchd-vlm-backfill.sh").read_text()
    for required in (
        "com.petcam.vlm-historical-backfill", "reporter.vlm_backfill_worker",
        "<key>StartInterval</key><integer>3600</integer>", "<key>RunAtLoad</key><true/>",
        "<key>USER</key><string>$RUN_USER</string>", "<key>LOGNAME</key><string>$RUN_USER</string>",
        "VLM_PROVIDER</key><string>claude_cli_batch", "ANTHROPIC_MODEL_EXACT</key><string>claude-sonnet-5",
        "REGISTER_HIGHLIGHTS</key><string>0", "plutil -lint",
    ):
        assert required in text
    assert "SUPABASE_SERVICE_ROLE_KEY</key>" not in text
    assert "R2_SECRET_ACCESS_KEY</key>" not in text
```

- [ ] **Step 2: RED를 확인한다**

Run: `uv run pytest tests/test_install_vlm_backfill.py -q`  
Expected: FAIL because installer does not exist.

- [ ] **Step 3: installer를 구현한다**

기존 `install-launchd-vlm-candidate.sh` 패턴을 복사하되 label·entrypoint·hourly schedule만 분리한다. 설치 전 `claude auth status`와 checkpoint 파일을 fail-fast 검사하되 auth JSON을 stdout/stderr에 출력하지 않는다. plist에는 비밀값을 복사하지 않고 기존 config loader가 repo root `.env`를 읽게 한다. plist를 `plutil -lint`한 뒤 bootstrap하고, 출력에는 설치 경로·다음 실행·롤백 명령만 표시한다. 이미 240개가 완료된 상태에서 재설치해도 worker는 no-op이어야 한다. 수동 canary 직후 `RunAtLoad`가 실행돼도 worker의 55분 cooldown 때문에 다음 source night는 즉시 시작하지 않는다.

- [ ] **Step 4: shell/plist/전체 테스트를 통과시킨다**

Run: `bash -n install-launchd-vlm-backfill.sh`  
Run: `uv run pytest tests/test_install_vlm_backfill.py tests/test_install_vlm_launchd.py -q`  
Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add install-launchd-vlm-backfill.sh tests/test_install_vlm_backfill.py .env.example
git commit -m "feat: 시간당 VLM 과거 백필 LaunchAgent"
```

---

### Task 6: read-only preview, 첫 30개 canary, 8시간 가동

**Files:**
- Create: `scripts/preview_vlm_backfill.py`
- Create: `tests/test_preview_vlm_backfill.py`
- Modify: `specs/2026-07-15-historical-vlm-backfill-plan.md`

**Interfaces:**
- CLI: `uv run python scripts/preview_vlm_backfill.py --source-date 2026-07-07`
- Output: 30개 clip의 축약 ID·bucket·slot·Gate source(existing/ephemeral) JSON.
- Preview는 selector run/job·activity evidence·Claude 호출을 모두 0으로 유지한다.

- [ ] **Step 1: preview no-write 테스트를 작성한다**

```python
def test_preview_returns_30_without_db_or_claude_write(capsys):
    sb = FakeSB(raw_store())
    main(["--source-date", "2026-07-07"], sb=sb, process_fn=must_not_call)
    assert len(json.loads(capsys.readouterr().out)["selected"]) == 30
    assert sb.store.get("clip_vlm_selector_runs", []) == []
    assert sb.store.get("clip_vlm_jobs", []) == []

def test_preview_rejects_dates_outside_frozen_allowlist():
    with pytest.raises(SystemExit):
        main(["--source-date", "2026-07-15"], sb=FakeSB(raw_store()))
```

- [ ] **Step 2: RED→GREEN 후 preview 스크립트를 커밋한다**

Run: `uv run pytest tests/test_preview_vlm_backfill.py -q`  
Expected before implementation: FAIL. Expected after implementation: PASS.

```bash
git add scripts/preview_vlm_backfill.py tests/test_preview_vlm_backfill.py
git commit -m "feat: VLM 과거 백필 read-only preview"
```

- [ ] **Step 3: 전체 검증을 실행한다**

Run: `uv run pytest -q`  
Run: `uv run python -m compileall -q reporter scripts`  
Run: `bash -n install-launchd-vlm-backfill.sh`  
Run: `git diff --check`  
Run: `git check-ignore .env storage/`  
Expected: all exit 0.

- [ ] **Step 4: production preflight와 preview를 read-only로 실행한다**

```bash
uv run python scripts/preview_vlm_backfill.py --source-date 2026-07-07 > storage/vlm-backfill-20260707-14/preview-20260707.json
```

확인: target camera가 기간 최다 카메라, selected=30, 시간 bucket quota 합계=30, clip 중복=0, 기존 VLM identity 중복=0, DB run/job count 불변.

- [ ] **Step 5: 첫 wave를 수동 canary로 실행한다**

```bash
uv run python -m reporter.vlm_backfill_worker
```

확인: source date=2026-07-07, selector runs=8, jobs=30, requested/actual model exact Sonnet 5, actual cost=0, 앱/GT/activity write=0, 임시 MP4=0. 안전 조건 하나라도 실패하면 LaunchAgent를 설치하지 않는다.

- [ ] **Step 6: canary 통과 후 시간당 LaunchAgent를 설치한다**

```bash
chmod +x install-launchd-vlm-backfill.sh
./install-launchd-vlm-backfill.sh
```

`launchctl print gui/$(id -u)/com.petcam.vlm-historical-backfill`에서 env·StartInterval·last exit를 확인한다. 첫 canary가 완료됐으므로 이후 일곱 source night를 시간당 하나씩 처리한다.

- [ ] **Step 7: 배포 상태를 기록하고 push한다**

```bash
git add specs/2026-07-15-historical-vlm-backfill-plan.md
git commit -m "docs: VLM 240개 백필 canary와 가동 상태 기록"
git push origin main
```

---

### Task 7: 240개 최종 보고와 SOT

**Files:**
- Create: `scripts/report_vlm_backfill.py`
- Create: `tests/test_report_vlm_backfill.py`
- Modify: `specs/2026-07-15-historical-vlm-backfill-design.md`
- Modify: `specs/2026-07-15-historical-vlm-backfill-plan.md`
- Modify: `/Users/baek/petcam-lab/specs/next-session.md`

**Interfaces:**
- CLI: `uv run python scripts/report_vlm_backfill.py --out storage/vlm-backfill-20260707-14`
- Produces: `REPORT.md`, `jobs.json`, source night별 contact sheet 8개.

- [ ] **Step 1: 집계 정합 테스트를 작성한다**

```python
def test_report_counts_dates_slots_actions_costs():
    report = aggregate(fake_240_jobs())
    assert report.total == 240
    assert report.by_date == {f"2026-07-{d:02d}": 30 for d in range(7, 15)}
    assert report.actual_cost_usd == 0
    assert sum(report.by_action.values()) == 240

def test_report_output_must_stay_under_repo_storage(tmp_path):
    with pytest.raises(ValueError, match="storage"):
        validate_output_path(Path("/tmp/outside"))
```

- [ ] **Step 2: RED→GREEN 후 보고 스크립트를 구현한다**

contact sheet는 source night당 최대 30개를 5×6 grid로 만들고 clip8·slot·action·confidence만 표시한다. 전체 UUID·owner ID·이메일·R2 credential은 출력하지 않는다. `--out`은 resolve 후 반드시 repo `storage/` 아래인지 검사한다. 원본 MP4는 `TemporaryDirectory`에서만 내려받고 완료 후 잔존 0을 assert한다.

Run: `uv run pytest tests/test_report_vlm_backfill.py -q`  
Expected: PASS after implementation.

- [ ] **Step 3: 8시간 종료 후 보고서를 생성하고 운영 상태를 검증한다**

```bash
uv run python scripts/report_vlm_backfill.py --out storage/vlm-backfill-20260707-14
```

확인: 날짜별 30, 총 240, terminal/held 명시, model mismatch 0, actual cost 0, equivalent cost 합계, temp MP4 0. `launchctl bootout gui/$(id -u)/com.petcam.vlm-historical-backfill`로 임시 worker를 내리고 plist를 제거한다. 정규 `com.petcam.vlm-candidate-worker`와 `com.petcam.activity-worker`는 유지한다.

- [ ] **Step 4: SOT를 갱신하고 전체 회귀를 실행한다**

Run: `uv run pytest -q` in `/Users/baek/petcam-nightly-reporter`  
Run: `uv run pytest -q` in `/Users/baek/petcam-lab`  
Run: `git diff --check` in both repositories.  
Expected: all exit 0.

- [ ] **Step 5: 커밋·push하고 사용자에게 최종 보고한다**

```bash
git add scripts/report_vlm_backfill.py tests/test_report_vlm_backfill.py specs/2026-07-15-historical-vlm-backfill-design.md specs/2026-07-15-historical-vlm-backfill-plan.md
git commit -m "feat: VLM 240개 백필 최종 보고"
git push origin main
```

petcam-lab에서는 `specs/next-session.md`만 별도 `docs:` 커밋·push한다.
