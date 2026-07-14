# 예산 고정형 Claude VLM 후보 라우터 v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 밤 20~04시를 2시간 구간으로 나눠 카메라별 최대 4개 후보만 직접 Anthropic Messages API로 분석하고, 선정 근거·모델·token·비용을 재현 가능하게 저장하는 shadow 파이프라인을 만든다.

**Architecture:** `petcam-lab`에는 selector run/job 원장을 forward migration과 원자 RPC로 추가한다. `petcam-nightly-reporter`는 기존 activity worker와 분리된 entrypoint에서 metadata-only episode/slot selector를 실행하고, durable job 저장 뒤에만 직접 이미지 API를 호출한다. 결과는 shadow 테이블에만 남기며 `behavior_logs`, 앱 하이라이트, 활동시간에는 반영하지 않는다.

**Tech Stack:** Python 3.12, pytest, Supabase/PostgreSQL/RLS, Anthropic Python SDK Messages API, OpenCV/ffmpeg, launchd, uv.

## Global Constraints

- 트리거는 KST `22:00 / 00:00 / 02:00 / 04:00`, 분석 구간은 각각 직전 2시간이다. `04:00~06:00`은 v1 범위 밖이다.
- 카메라·구간 최대 4개, 카메라·밤 최대 16개, 전체 밤 최대 64개이며 적합 후보가 없으면 빈 슬롯을 유지한다.
- 슬롯 순서는 `customer_highlight`, `subtle_behavior`, `diversity_discovery`, `exclusion_audit`다.
- `exclude_absent`, `exclude_static`, `unknown`, `gecko_visible=false`, 낮은 `motion_score`는 hard skip 사유가 아니다.
- hard skip은 입력 무효, 동일 selector job 존재, 동일 analyzer 결과 존재, 같은 episode 대표 중복으로만 제한한다.
- 월 API hard cap은 `$10.00`; 원장 조회 실패·cap 초과·인증/결제 오류·model mismatch·연속 retryable 실패 3건·usage/schema 누락이면 신규 호출을 중단한다.
- 운영 analyzer는 Claude Code CLI가 아니라 Anthropic Messages API를 사용한다. 모델은 exact ID `claude-sonnet-5`로 고정하고 alias를 허용하지 않는다.
- 입력은 시간순 JPEG 6장, 긴 변 768px, no-upscale, quality 85, 출력 `max_tokens=256`, `temperature=0`, JSON schema다.
- durable run/job 저장 전 API 호출을 금지한다. API 성공 후 DB 저장 실패 시 자동 재호출도 금지한다.
- shadow 동안 `behavior_logs`, `camera_clips`, 앱 하이라이트, 고객 알림, Flutter, 원본 `motion_clips`/R2를 변경하지 않는다.
- 기존 `reporter.activity_worker`, `install-launchd-activity.sh`, `com.petcam.activity-worker`는 변경·중단하지 않는다.
- production migration 적용, 30개 유료 API 비교, launchd 유료 shadow 활성화는 각각 사용자 승인 뒤에만 수행한다.
- 패키지 설치는 `uv add`만 사용한다. 비밀값은 `.env`에만 두고 로그·DB·커밋에 넣지 않는다.

---

## File Map

### `petcam-lab`

- `migrations/2026-07-15_clip_vlm_candidate_jobs.sql`: run/job 테이블, RLS, 원자 생성 RPC.
- `tests/test_clip_vlm_candidate_migration_contract.py`: migration 정적 계약 회귀 테스트.
- `docs/DATABASE.md`: 새 테이블과 RLS/수명주기 문서.
- `specs/next-session.md`: shadow 상태와 다음 승인 경계.
- `.claude/donts-audit.md`: Standard 작업 검수 한 줄.

### `petcam-nightly-reporter`

- `reporter/vlm_models.py`: selector/job dataclass와 enum.
- `reporter/vlm_candidate_indexer.py`: window clip + activity/Gate/job 이력 조회.
- `reporter/vlm_episode.py`: 사분위·bbox bucket·120초 episode 대표 결정.
- `reporter/vlm_selector.py`: 네 슬롯의 독립 pool/rank/선택.
- `reporter/vlm_store.py`: 원자 run/job 생성과 상태 전이.
- `reporter/vlm_budget.py`: 가격표·월 원장·공정 처리 순서·circuit breaker.
- `reporter/vlm_frames.py`: 직접 API 전용 6장/768px/quality85 sampler.
- `reporter/anthropic_analyzer.py`: Messages API 요청·structured output·provenance.
- `reporter/vlm_candidate_worker.py`: selection과 analysis orchestration.
- `scripts/replay_vlm_selector.py`: 과거 window offline replay.
- `scripts/select_vlm_gt30.py`: 사람 확인용 30개 manifest 생성.
- `scripts/eval_vlm_direct_api.py`: 기존 결과와 직접 API 품질/token 비교.
- `install-launchd-vlm-candidate.sh`: 별도 LaunchAgent 설치기.
- `tests/_fakes.py`: RPC/update/order-desc를 지원하는 Supabase fake.
- `tests/test_vlm_*.py`: 각 순수 단위와 orchestration 회귀.
- `.env.example`: 비밀값 없는 VLM 설정 설명.
- `.gitignore`: `/storage/` 평가 산출물 제외.

---

### Task 1: Forward DB migration과 원자 run/job 생성 계약

**Files:**
- Create: `/Users/baek/petcam-lab/migrations/2026-07-15_clip_vlm_candidate_jobs.sql`
- Create: `/Users/baek/petcam-lab/tests/test_clip_vlm_candidate_migration_contract.py`

**Interfaces:**
- Consumes: `cameras(id, owner_id)`, `motion_clips(id, camera_id, owner_id)`, `clip_prelabels(id)`, `clip_activity_assessments(id)`.
- Produces: `clip_vlm_selector_runs`, `clip_vlm_jobs`, 원자 생성 RPC `fn_create_clip_vlm_selector_run(...)`, 원자 비용예약 RPC `fn_reserve_clip_vlm_job(...)`.

- [ ] **Step 1: migration 계약의 failing test를 작성한다**

```python
from pathlib import Path

SQL = Path("migrations/2026-07-15_clip_vlm_candidate_jobs.sql")


def test_vlm_job_migration_contains_safety_contracts():
    text = SQL.read_text()
    required = [
        "unique (camera_id, window_start, selector_version)",
        "unique (clip_id, selector_version)",
        "unique (selector_run_id, slot)",
        "jsonb_array_length(p_jobs) > 4",
        "fn_create_clip_vlm_selector_run",
        "fn_reserve_clip_vlm_job",
        "grant execute on function public.fn_create_clip_vlm_selector_run(jsonb, jsonb) to service_role",
        "owner reads own vlm selector runs",
        "owner reads own vlm jobs",
        "revoke all on public.clip_vlm_jobs from anon, authenticated",
    ]
    assert all(item in text.lower() for item in required)


def test_vlm_job_status_and_slot_enums_are_closed():
    text = SQL.read_text().lower()
    assert "held_model_mismatch" in text
    assert "held_budget" in text
    assert "customer_highlight" in text
    assert "exclusion_audit" in text
```

- [ ] **Step 2: test가 파일 부재로 실패하는지 확인한다**

Run: `cd /Users/baek/petcam-lab && uv run pytest tests/test_clip_vlm_candidate_migration_contract.py -q`

Expected: `FileNotFoundError` 또는 두 test FAIL.

- [ ] **Step 3: migration을 작성한다**

```sql
create table public.clip_vlm_selector_runs (
  id uuid primary key default gen_random_uuid(),
  camera_id uuid not null references public.cameras(id) on delete cascade,
  window_start timestamptz not null,
  window_end timestamptz not null,
  selector_version text not null,
  clips_seen integer not null check (clips_seen >= 0),
  hard_invalid_count integer not null check (hard_invalid_count >= 0),
  already_processed_count integer not null check (already_processed_count >= 0),
  episode_count integer not null check (episode_count >= 0),
  pool_counts jsonb not null default '{}'::jsonb,
  selected_clip_ids jsonb not null default '[]'::jsonb,
  unselected_reason_counts jsonb not null default '{}'::jsonb,
  monthly_budget_usd numeric(12,6) not null,
  month_reserved_usd numeric(12,6) not null,
  month_actual_usd numeric(12,6) not null,
  producer_host text not null,
  producer_run_id text not null,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (camera_id, window_start, selector_version),
  check (window_end > window_start)
);

create table public.clip_vlm_jobs (
  id uuid primary key default gen_random_uuid(),
  selector_run_id uuid not null references public.clip_vlm_selector_runs(id) on delete cascade,
  clip_id uuid not null references public.motion_clips(id) on delete cascade,
  camera_id uuid not null references public.cameras(id) on delete cascade,
  window_start timestamptz not null,
  window_end timestamptz not null,
  slot text not null check (slot in ('customer_highlight','subtle_behavior','diversity_discovery','exclusion_audit')),
  selector_version text not null,
  episode_key text not null,
  rank_features jsonb not null,
  selection_reason text not null,
  activity_assessment_id uuid references public.clip_activity_assessments(id) on delete set null,
  prelabel_id uuid references public.clip_prelabels(id) on delete set null,
  status text not null check (status in ('queued','submitted','succeeded','failed_retryable','failed_terminal','held_budget','held_model_mismatch')),
  attempt_count integer not null default 0 check (attempt_count between 0 and 2),
  queued_at timestamptz not null default now(),
  submitted_at timestamptz,
  completed_at timestamptz,
  model_requested text not null,
  model_actual text,
  prompt_version text not null,
  prompt_sha256 text not null,
  sampler_version text not null,
  frames_sampled integer check (frames_sampled between 0 and 6),
  provider_request_id text,
  result jsonb,
  error_code text,
  reserved_cost_usd numeric(12,6) not null check (reserved_cost_usd >= 0),
  input_tokens bigint,
  cache_creation_input_tokens bigint,
  cache_read_input_tokens bigint,
  output_tokens bigint,
  cost_usd numeric(12,6),
  pricing_version text not null,
  producer_host text not null,
  producer_run_id text not null,
  created_at timestamptz not null default now(),
  unique (clip_id, selector_version),
  unique (selector_run_id, slot),
  check (window_end > window_start)
);

create index idx_clip_vlm_jobs_status_queued on public.clip_vlm_jobs(status, queued_at);
create index idx_clip_vlm_jobs_month_cost on public.clip_vlm_jobs(created_at, status);
create index idx_clip_vlm_jobs_camera_created on public.clip_vlm_jobs(camera_id, created_at desc);

alter table public.clip_vlm_selector_runs enable row level security;
alter table public.clip_vlm_jobs enable row level security;

create policy "owner reads own vlm selector runs" on public.clip_vlm_selector_runs
  for select to authenticated using (
    exists (select 1 from public.cameras c
            where c.id = clip_vlm_selector_runs.camera_id and c.owner_id = auth.uid())
  );
create policy "owner reads own vlm jobs" on public.clip_vlm_jobs
  for select to authenticated using (
    exists (select 1 from public.motion_clips mc
            where mc.id = clip_vlm_jobs.clip_id and mc.owner_id = auth.uid())
  );

revoke all on public.clip_vlm_selector_runs from anon, authenticated;
revoke all on public.clip_vlm_jobs from anon, authenticated;
grant select on public.clip_vlm_selector_runs to authenticated;
grant select on public.clip_vlm_jobs to authenticated;
grant all on public.clip_vlm_selector_runs to service_role;
grant all on public.clip_vlm_jobs to service_role;

create or replace function public.fn_create_clip_vlm_selector_run(p_run jsonb, p_jobs jsonb)
returns uuid language plpgsql security invoker set search_path = public, pg_temp as $$
declare v_run_id uuid; v_job jsonb;
begin
  if jsonb_typeof(p_jobs) is distinct from 'array' or jsonb_array_length(p_jobs) > 4 then
    raise exception 'p_jobs must be an array with at most 4 rows' using errcode = '22023';
  end if;
  insert into public.clip_vlm_selector_runs (
    camera_id, window_start, window_end, selector_version, clips_seen,
    hard_invalid_count, already_processed_count, episode_count, pool_counts,
    selected_clip_ids, unselected_reason_counts, monthly_budget_usd,
    month_reserved_usd, month_actual_usd, producer_host, producer_run_id, completed_at
  ) values (
    (p_run->>'camera_id')::uuid, (p_run->>'window_start')::timestamptz,
    (p_run->>'window_end')::timestamptz, p_run->>'selector_version',
    (p_run->>'clips_seen')::integer, (p_run->>'hard_invalid_count')::integer,
    (p_run->>'already_processed_count')::integer, (p_run->>'episode_count')::integer,
    p_run->'pool_counts', p_run->'selected_clip_ids', p_run->'unselected_reason_counts',
    (p_run->>'monthly_budget_usd')::numeric, (p_run->>'month_reserved_usd')::numeric,
    (p_run->>'month_actual_usd')::numeric, p_run->>'producer_host',
    p_run->>'producer_run_id', now()
  ) on conflict (camera_id, window_start, selector_version) do update set
    pool_counts = excluded.pool_counts,
    selected_clip_ids = excluded.selected_clip_ids,
    unselected_reason_counts = excluded.unselected_reason_counts,
    completed_at = now()
  returning id into v_run_id;

  for v_job in select value from jsonb_array_elements(p_jobs) loop
    insert into public.clip_vlm_jobs (
      selector_run_id, clip_id, camera_id, window_start, window_end, slot,
      selector_version, episode_key, rank_features, selection_reason,
      activity_assessment_id, prelabel_id, status, model_requested,
      prompt_version, prompt_sha256, sampler_version, reserved_cost_usd,
      pricing_version, producer_host, producer_run_id
    ) values (
      v_run_id, (v_job->>'clip_id')::uuid, (p_run->>'camera_id')::uuid,
      (p_run->>'window_start')::timestamptz, (p_run->>'window_end')::timestamptz,
      v_job->>'slot', p_run->>'selector_version', v_job->>'episode_key',
      v_job->'rank_features', v_job->>'selection_reason',
      nullif(v_job->>'activity_assessment_id','')::uuid,
      nullif(v_job->>'prelabel_id','')::uuid, 'queued',
      v_job->>'model_requested', v_job->>'prompt_version', v_job->>'prompt_sha256',
      v_job->>'sampler_version', (v_job->>'reserved_cost_usd')::numeric,
      v_job->>'pricing_version', p_run->>'producer_host', p_run->>'producer_run_id'
    ) on conflict do nothing;
  end loop;
  return v_run_id;
end $$;

revoke all on function public.fn_create_clip_vlm_selector_run(jsonb, jsonb) from public, anon, authenticated;
grant execute on function public.fn_create_clip_vlm_selector_run(jsonb, jsonb) to service_role;

create or replace function public.fn_reserve_clip_vlm_job(
  p_job_id uuid, p_month_start timestamptz, p_budget_usd numeric
) returns boolean language plpgsql security invoker set search_path = public, pg_temp as $$
declare v_job public.clip_vlm_jobs%rowtype; v_committed numeric;
begin
  perform pg_advisory_xact_lock(hashtextextended('clip_vlm_monthly_budget', 0));
  select * into v_job from public.clip_vlm_jobs where id = p_job_id for update;
  if not found or v_job.status not in ('queued','failed_retryable') then
    raise exception 'job is not reservable' using errcode = '22023';
  end if;
  select coalesce(sum(coalesce(cost_usd, 0)), 0)
       + coalesce(sum(case when status in ('submitted','failed_retryable')
                           then reserved_cost_usd else 0 end), 0)
    into v_committed
    from public.clip_vlm_jobs where created_at >= p_month_start and id <> p_job_id;
  if v_committed + v_job.reserved_cost_usd > p_budget_usd then
    update public.clip_vlm_jobs set status='held_budget', completed_at=now() where id=p_job_id;
    return false;
  end if;
  update public.clip_vlm_jobs
    set status='submitted', attempt_count=attempt_count+1, submitted_at=now()
    where id=p_job_id;
  return true;
end $$;

revoke all on function public.fn_reserve_clip_vlm_job(uuid, timestamptz, numeric)
  from public, anon, authenticated;
grant execute on function public.fn_reserve_clip_vlm_job(uuid, timestamptz, numeric) to service_role;

-- 즉시 무력화: worker VLM_ROUTER_ENABLED=0. 완전 제거는 jobs → runs → function 순서로 drop한다.
```

- [ ] **Step 4: 정적 계약과 전체 backend test를 실행한다**

Run: `cd /Users/baek/petcam-lab && uv run pytest tests/test_clip_vlm_candidate_migration_contract.py -q && uv run pytest -q`

Expected: 새 test 2개와 기존 334개가 모두 PASS. 이 단계에서는 production DB에 적용하지 않는다.

- [ ] **Step 5: migration만 커밋한다**

```bash
git add migrations/2026-07-15_clip_vlm_candidate_jobs.sql tests/test_clip_vlm_candidate_migration_contract.py
git commit -m "feat: VLM 후보 라우터 job 원장 스키마"
```

---

### Task 2: VLM domain types, config, 고정 시간창

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_models.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/config.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/timewin.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_models.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_timewin.py`

**Interfaces:**
- Produces: `Slot`, `JobStatus`, `CandidateClip`, `SelectedCandidate`, `trigger_window(now)`.

- [ ] **Step 1: enum/시간창 failing tests를 작성한다**

```python
def test_trigger_window_maps_four_kst_runs():
    cases = [(22, 20), (0, 22), (2, 0), (4, 2)]
    for trigger_hour, start_hour in cases:
        day = 15 if trigger_hour != 0 else 16
        now = datetime(2026, 7, day, trigger_hour, 7, tzinfo=ZoneInfo("Asia/Seoul"))
        start, end = trigger_window(now)
        assert end.astimezone(ZoneInfo("Asia/Seoul")).hour == trigger_hour
        assert start.astimezone(ZoneInfo("Asia/Seoul")).hour == start_hour
        assert end - start == timedelta(hours=2)


def test_trigger_window_rejects_unscheduled_hour():
    with pytest.raises(ValueError, match="22, 0, 2, 4"):
        trigger_window(datetime(2026, 7, 15, 21, tzinfo=ZoneInfo("Asia/Seoul")))
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `cd /Users/baek/petcam-nightly-reporter && uv run pytest tests/test_vlm_models.py tests/test_timewin.py -q`

Expected: import error로 FAIL.

- [ ] **Step 3: 실제 타입과 설정을 추가한다**

```python
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Slot(str, Enum):
    CUSTOMER_HIGHLIGHT = "customer_highlight"
    SUBTLE_BEHAVIOR = "subtle_behavior"
    DIVERSITY_DISCOVERY = "diversity_discovery"
    EXCLUSION_AUDIT = "exclusion_audit"


class JobStatus(str, Enum):
    QUEUED = "queued"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    HELD_BUDGET = "held_budget"
    HELD_MODEL_MISMATCH = "held_model_mismatch"


@dataclass(frozen=True, slots=True)
class CandidateClip:
    id: str
    camera_id: str
    started_at: datetime
    duration_sec: float
    r2_key: str | None
    motion_score: float
    width: int | None
    height: int | None
    assessment_id: str | None = None
    activity_decision: str | None = None
    prelabel_id: str | None = None
    gecko_visible: bool | None = None
    visibility_confidence: float | None = None
    gecko_bbox: tuple[float, float, float, float] | None = None
    motion_metrics: dict[str, object] = field(default_factory=dict)
    hard_skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SelectedCandidate:
    clip: CandidateClip
    slot: Slot
    episode_key: str
    rank_features: dict[str, object]
    selection_reason: str
```

`config.py`에는 `from decimal import Decimal`과 다음 값만 추가한다.

```python
VLM_ROUTER_ENABLED = os.environ.get("VLM_ROUTER_ENABLED", "0") == "1"
VLM_SELECTOR_VERSION = "budget-router-v1"
VLM_ACTIVITY_POLICY_VERSION = "activity-v1"
VLM_MODEL = os.environ.get("ANTHROPIC_MODEL_EXACT", "claude-sonnet-5")
VLM_PROMPT_VERSION = "v4.0-direct-images"
VLM_SAMPLER_VERSION = "six-768q85-v1"
VLM_MONTHLY_BUDGET_USD = Decimal(os.environ.get("VLM_MONTHLY_BUDGET_USD", "10.00"))
VLM_RESERVED_COST_USD = Decimal(os.environ.get("VLM_RESERVED_COST_USD", "0.10"))
VLM_MAX_PER_CAMERA_WINDOW = 4
VLM_MAX_PER_CAMERA_NIGHT = 16
VLM_MAX_PER_NIGHT = 64
VLM_FRAMES = 6
VLM_FRAME_LONG_EDGE = 768
VLM_JPEG_QUALITY = 85
```

`timewin.py`에는 `from zoneinfo import ZoneInfo`를 추가하고 분을 버려 고정 trigger 경계를 만드는 함수를 추가한다.

```python
def trigger_window(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo("Asia/Seoul"))
    if local.hour not in {22, 0, 2, 4}:
        raise ValueError("VLM trigger hour must be one of 22, 0, 2, 4 KST")
    end_local = local.replace(minute=0, second=0, microsecond=0)
    return ((end_local - timedelta(hours=2)).astimezone(timezone.utc),
            end_local.astimezone(timezone.utc))
```

- [ ] **Step 4: unit tests를 통과시킨다**

Run: `uv run pytest tests/test_vlm_models.py tests/test_timewin.py -q`

Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_models.py reporter/config.py reporter/timewin.py tests/test_vlm_models.py tests/test_timewin.py
git commit -m "feat: VLM 후보 라우터 도메인과 시간창"
```

---

### Task 3: Candidate indexer와 hard eligibility

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_candidate_indexer.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/_fakes.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_candidate_indexer.py`

**Interfaces:**
- Consumes: `CandidateClip`, Supabase tables.
- Produces: `load_window_candidates(sb, start, end, policy_version, selector_version) -> list[CandidateClip]`, `partition_eligibility(clips)`, `load_recent_selection_history(sb, camera_id, since) -> dict[str, int]`.

- [ ] **Step 1: pagination·join·hard skip tests를 작성한다**

```python
def test_loader_keeps_absent_static_unknown_and_joins_dimensions():
    sb = FakeSB(seed_rows())
    clips = load_window_candidates(sb, START, END, "activity-v1", "budget-router-v1", page_size=2)
    assert [c.activity_decision for c in clips] == ["exclude_absent", "exclude_static", "unknown"]
    assert clips[0].width == 1280 and clips[0].height == 720


def test_partition_only_hard_skips_invalid_or_existing_identity():
    eligible, reasons = partition_eligibility(make_candidates())
    assert {c.id for c in eligible} == {"absent", "static", "unknown", "low-motion"}
    assert reasons == {"invalid_input": 2, "already_selector_job": 1}
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_vlm_candidate_indexer.py -q`

Expected: module import FAIL.

- [ ] **Step 3: loader를 구현한다**

```python
def load_window_candidates(sb, start, end, policy_version, selector_version, *, page_size=500):
    motion_rows = _paged(
        lambda lo, hi: sb.table("motion_clips")
        .select("id,camera_id,started_at,duration_sec,r2_key,motion_score,width,height")
        .gte("started_at", start.isoformat()).lt("started_at", end.isoformat())
        .order("started_at").range(lo, hi).execute().data,
        page_size,
    )
    ids = [row["id"] for row in motion_rows]
    assessments = _chunks_query(sb, "clip_activity_assessments", "clip_id", ids,
                                extra=lambda q: q.eq("policy_version", policy_version))
    prelabel_ids = [row["prelabel_id"] for row in assessments]
    prelabels = _chunks_query(sb, "clip_prelabels", "id", prelabel_ids)
    existing = _chunks_query(sb, "clip_vlm_jobs", "clip_id", ids)
    return _merge_rows(motion_rows, assessments, prelabels, existing)


def partition_eligibility(clips):
    eligible, reasons = [], Counter()
    for clip in clips:
        if clip.duration_sec <= 0 or not clip.r2_key:
            reasons["invalid_input"] += 1
        elif clip.hard_skip_reason:
            reasons[clip.hard_skip_reason] += 1
        else:
            eligible.append(clip)
    return eligible, dict(reasons)
```

`_merge_rows`는 동일 selector의 `queued/submitted/succeeded` job이면 `hard_skip_reason='already_selector_job'`, selector가 달라도 동일 `model_requested + prompt_version + sampler_version`의 succeeded job이면 `hard_skip_reason='already_analyzed_identity'`로 만든다. failed job은 새 job을 만들지 않고 Task 6의 기존 job 재시도/종료 경로로 넘긴다. `_paged`는 page가 `page_size`보다 작을 때 종료하고, `_chunks_query`는 UUID를 200개씩 `.in_()` 조회한다. row 단위 파싱 오류는 `malformed_evidence` count로 격리하되 clip 자체는 evidence 없는 unknown 후보로 남긴다. `load_recent_selection_history`는 최근 7일 `clip_vlm_jobs.rank_features->diversity_bucket`을 Python에서 count해 `{bucket: count}`를 반환한다. `tests/_fakes.py`의 `.order(col, desc=False)`와 `.update(row)`를 실제 supabase-py chaining과 같은 모양으로 확장한다.

- [ ] **Step 4: indexer와 기존 fake 사용 tests를 모두 실행한다**

Run: `uv run pytest tests/test_vlm_candidate_indexer.py tests/test_activity_indexer.py tests/test_activity_store.py -q`

Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_candidate_indexer.py tests/_fakes.py tests/test_vlm_candidate_indexer.py
git commit -m "feat: VLM 후보용 evidence indexer"
```

---

### Task 4: Metadata-only episode dedup

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_episode.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_episode.py`

**Interfaces:**
- Consumes: `list[CandidateClip]`.
- Produces: `reduce_episodes(clips, window_start) -> list[EpisodeRepresentative]`.

- [ ] **Step 1: 경계 tests를 작성한다**

```python
def test_same_episode_requires_all_four_conditions():
    clips = [clip("a", sec=0, decision="active", score=1, bbox=(0, 0, 100, 100)),
             clip("b", sec=110, decision="active", score=1, bbox=(10, 10, 100, 100))]
    reps = reduce_episodes(clips, START)
    assert len(reps) == 1


def test_121_seconds_or_different_decision_splits_episode():
    assert len(reduce_episodes([clip("a", sec=0), clip("b", sec=121)], START)) == 2
    assert len(reduce_episodes([clip("a", sec=0, decision="active"),
                                clip("b", sec=10, decision="unknown")], START)) == 2


def test_bbox_grid_uses_motion_clip_dimensions():
    assert bbox_bucket((0, 0, 100, 100), 1280, 720) == (0, 0, "small")
    assert bbox_bucket(None, 1280, 720) == ("none", "none", "none")
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_vlm_episode.py -q`

Expected: module import FAIL.

- [ ] **Step 3: episode key와 대표 결정 함수를 구현한다**

```python
@dataclass(frozen=True, slots=True)
class EpisodeRepresentative:
    clip: CandidateClip
    episode_key: str
    motion_quartile: int
    bbox_bucket: tuple[int | str, int | str, str]


def bbox_bucket(bbox, width, height):
    if bbox is None or not width or not height:
        return ("none", "none", "none")
    x, y, w, h = bbox
    col = min(2, max(0, int(((x + w / 2) / width) * 3)))
    row = min(2, max(0, int(((y + h / 2) / height) * 3)))
    ratio = (w * h) / (width * height)
    size = "small" if ratio < 0.05 else "medium" if ratio < 0.20 else "large"
    return (row, col, size)


def same_episode(previous, current):
    return (
        (current.clip.started_at - previous.clip.started_at).total_seconds() <= 120
        and current.clip.activity_decision == previous.clip.activity_decision
        and current.motion_quartile == previous.motion_quartile
        and current.bbox_bucket == previous.bbox_bucket
    )


def _representative(group, window_start):
    middle = group[len(group) // 2].clip.started_at
    return max(group, key=lambda item: (
        int(item.clip.prelabel_id is not None and bool(item.clip.motion_metrics)),
        int(item.clip.duration_sec > 0 and item.clip.r2_key is not None),
        -abs((item.clip.started_at - middle).total_seconds()),
        hashlib.sha256(f"{item.clip.camera_id}:{window_start.isoformat()}:{item.clip.id}".encode()).hexdigest(),
    ))
```

사분위는 nearest-rank `25/50/75%` 경계와 `bisect_right`로 계산해 같은 score가 항상 같은 bucket을 갖게 한다. episode key는 `sha256(camera_id|first_started_at|decision|motion_quartile|bbox_bucket)[:24]`다.

- [ ] **Step 4: tests를 통과시킨다**

Run: `uv run pytest tests/test_vlm_episode.py -q`

Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_episode.py tests/test_vlm_episode.py
git commit -m "feat: VLM 후보 episode 중복 제거"
```

---

### Task 5: 네 슬롯의 독립 selector

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_selector.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_selector.py`

**Interfaces:**
- Consumes: episode 대표, 최근 7일 bucket count.
- Produces: `select_candidates(representatives, history, window_start) -> list[SelectedCandidate]`.

- [ ] **Step 1: 목적별 회귀 tests를 작성한다**

```python
def test_four_slots_do_not_duplicate_clip_or_episode():
    selected = select_candidates(representatives(), history={}, window_start=START)
    assert [s.slot.value for s in selected] == [
        "customer_highlight", "subtle_behavior", "diversity_discovery", "exclusion_audit"
    ]
    assert len({s.clip.id for s in selected}) == len(selected)
    assert len({s.episode_key for s in selected}) == len(selected)


def test_absent_static_unknown_remain_in_audit_pool():
    selected = select_candidates(audit_only_representatives(), {}, START)
    audit = next(s for s in selected if s.slot is Slot.EXCLUSION_AUDIT)
    assert audit.clip.activity_decision in {"exclude_absent", "exclude_static", "unknown"}


def test_large_motion_cannot_fill_all_slots():
    selected = select_candidates(moving_heavy_representatives(), {}, START)
    assert sum(s.clip.activity_decision == "active" for s in selected) < 4


def test_same_input_is_deterministic_and_empty_pool_stays_empty():
    assert select_candidates(representatives(), {}, START) == select_candidates(representatives(), {}, START)
    assert select_candidates([], {}, START) == []
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_vlm_selector.py -q`

Expected: module import FAIL.

- [ ] **Step 3: slot별 pool과 rank를 구현한다**

```python
SLOT_ORDER = (
    Slot.CUSTOMER_HIGHLIGHT,
    Slot.SUBTLE_BEHAVIOR,
    Slot.DIVERSITY_DISCOVERY,
    Slot.EXCLUSION_AUDIT,
)


def select_candidates(representatives, history, window_start):
    remaining = list(representatives)
    chosen = []
    for slot in SLOT_ORDER:
        pool = _pool_for(slot, remaining, window_start)
        if not pool:
            continue
        ranked = sorted(pool, key=lambda item: _rank(slot, item, history), reverse=True)
        item = _deterministic_top3(slot, ranked[:3], window_start)
        chosen.append(_selected(slot, item, history))
        remaining = [r for r in remaining if r.clip.id != item.clip.id and r.episode_key != item.episode_key]
    return chosen


def _deterministic_top3(slot, ranked, window_start):
    weights = [3, 2, 1][:len(ranked)]
    digest = hashlib.sha256(f"{slot.value}:{window_start.isoformat()}".encode()).digest()
    pick = int.from_bytes(digest[:4], "big") % sum(weights)
    for item, weight in zip(ranked, weights, strict=True):
        if pick < weight:
            return item
        pick -= weight
    raise AssertionError("weighted choice exhausted")
```

`_pool_for`와 `_rank`의 exact 계약:

- `customer_highlight`: `active`이거나 bbox/ROI evidence가 있는 row. tuple은 `active`, evidence 존재, `roi_flow_mag`, `max_bbox_center_disp`, 최근 bucket 빈도 역순, `motion_score` 순이다.
- `subtle_behavior`: bbox 존재, `global_bg_change <= window median`, `roi_flow_mag >= window median`. tuple은 `roi/(global+0.001)`, `roi_flow_mag`, 최근 motion bucket 희소성 순이다.
- `diversity_discovery`: 모든 remaining. bucket은 `camera|2h-hour|decision|motion_quartile|bbox-row-col-size`; 최근 7일 count가 낮은 순으로 top3 weighted choice한다.
- `exclusion_audit`: `exclude_absent/exclude_static/unknown`. `int(window_start.timestamp() // 7200) % 3`부터 상태를 round-robin하고 비어 있으면 다음 상태로 이동한다.
- `rank_features`에는 위 원시값과 history count만 넣고 행동 라벨 추론값은 넣지 않는다.

- [ ] **Step 4: selector tests를 통과시킨다**

Run: `uv run pytest tests/test_vlm_selector.py tests/test_vlm_episode.py -q`

Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_selector.py tests/test_vlm_selector.py
git commit -m "feat: 목적별 VLM 후보 네 슬롯 선택"
```

---

### Task 6: Durable store와 상태 전이

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_store.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/_fakes.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_store.py`

**Interfaces:**
- Produces: `create_run_and_jobs`, `load_due_jobs`, `mark_submitted`, `mark_result`, `mark_failure`, `hold_budget`.

- [ ] **Step 1: atomicity·idempotency·transition tests를 작성한다**

```python
def test_create_run_calls_atomic_rpc_once_and_creates_at_most_four_jobs():
    sb = FakeSB()
    run_id = create_run_and_jobs(sb, run_payload(), four_jobs())
    assert run_id == "clip_vlm_selector_runs-0"
    assert len(sb.store["clip_vlm_jobs"]) == 4


def test_same_run_and_jobs_are_idempotent():
    sb = FakeSB()
    first = create_run_and_jobs(sb, run_payload(), four_jobs())
    second = create_run_and_jobs(sb, run_payload(), four_jobs())
    assert first == second
    assert len(sb.store["clip_vlm_jobs"]) == 4


def test_invalid_transition_is_rejected_before_db_write():
    with pytest.raises(ValueError, match="queued -> succeeded"):
        validate_transition("queued", "succeeded")
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_vlm_store.py -q`

Expected: import FAIL.

- [ ] **Step 3: RPC fake와 store를 구현한다**

```python
ALLOWED_TRANSITIONS = {
    "queued": {"submitted", "held_budget", "failed_terminal"},
    "submitted": {"succeeded", "failed_retryable", "failed_terminal", "held_model_mismatch"},
    "failed_retryable": {"submitted", "held_budget", "failed_terminal"},
}


def create_run_and_jobs(sb, run, jobs):
    if len(jobs) > 4:
        raise ValueError("one camera/window may create at most 4 jobs")
    response = sb.rpc("fn_create_clip_vlm_selector_run", {"p_run": run, "p_jobs": jobs}).execute()
    if not response.data:
        raise RuntimeError("selector run RPC returned no id")
    return response.data


def mark_submitted(sb, job):
    validate_transition(job["status"], "submitted")
    response = sb.rpc("fn_reserve_clip_vlm_job", {
        "p_job_id": job["id"],
        "p_month_start": kst_month_start_utc(datetime.now(timezone.utc)).isoformat(),
        "p_budget_usd": str(config.VLM_MONTHLY_BUDGET_USD),
    }).execute()
    return bool(response.data)
```

`mark_submitted`의 boolean이 false면 이미 `held_budget`이므로 API를 호출하지 않는다. `mark_result`는 provider id/result/usage/cost/model actual/frame count를 한 update에 쓰고, model mismatch면 `held_model_mismatch`, 같으면 `succeeded`로 완료한다. `mark_failure`는 attempt `<2`인 download/timeout/429/5xx만 `failed_retryable`, 나머지는 `failed_terminal`로 둔다. Fake RPC는 동일 `(camera_id, window_start, selector_version)` run과 `(clip_id, selector_version)` job을 중복 생성하지 않고 월 reservation을 원자적으로 계산하도록 실제 DB 함수를 모사한다.

- [ ] **Step 4: store tests를 통과시킨다**

Run: `uv run pytest tests/test_vlm_store.py tests/test_activity_store.py -q`

Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_store.py tests/_fakes.py tests/test_vlm_store.py
git commit -m "feat: VLM durable job 상태 원장"
```

---

### Task 7: 비용 원장, 공정 순서, circuit breaker

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_budget.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_budget.py`

**Interfaces:**
- Produces: `Usage`, `calculate_cost`, `load_month_ledger`, `can_submit`, `fair_job_order`, `fair_selection_cap`, `CircuitBreaker`.

- [ ] **Step 1: 가격·cap·fairness tests를 작성한다**

```python
def test_cost_includes_uncached_cache_write_read_and_output():
    usage = Usage(input_tokens=7000, cache_creation_input_tokens=3000,
                  cache_read_input_tokens=2000, output_tokens=100)
    assert calculate_cost(usage) == Decimal("0.022900")


def test_budget_fails_closed_on_query_error_or_cap_crossing():
    with pytest.raises(BudgetUnavailable):
        load_month_ledger(BrokenSB(), NOW)
    assert not can_submit(Ledger(actual=Decimal("9.98"), reserved=Decimal("0")),
                          Decimal("0.10"), Decimal("10.00"))


def test_fair_order_round_robins_cameras_before_next_slot():
    ordered = fair_job_order(jobs_for_cameras("a", "b"))
    assert [(j["camera_id"], j["slot"]) for j in ordered[:4]] == [
        ("a", "customer_highlight"), ("b", "customer_highlight"),
        ("a", "subtle_behavior"), ("b", "subtle_behavior"),
    ]


def test_global_night_cap_is_applied_camera_round_robin():
    capped = fair_selection_cap(selected_for_five_cameras(), remaining=4)
    assert len(capped) == 4
    assert len({item.clip.camera_id for item in capped}) == 4
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_vlm_budget.py -q`

Expected: import FAIL.

- [ ] **Step 3: intro 가격표와 breaker를 구현한다**

```python
@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Ledger:
    actual: Decimal
    reserved: Decimal


class BudgetUnavailable(RuntimeError):
    pass


PRICING_VERSION = "anthropic-sonnet5-intro-through-2026-08-31"
INPUT_PER_MTOK = Decimal("2.00")
OUTPUT_PER_MTOK = Decimal("10.00")
CACHE_WRITE_5M_PER_MTOK = Decimal("2.50")
CACHE_READ_PER_MTOK = Decimal("0.20")


def calculate_cost(usage):
    million = Decimal(1_000_000)
    value = (
        Decimal(usage.input_tokens) * INPUT_PER_MTOK
        + Decimal(usage.cache_creation_input_tokens) * CACHE_WRITE_5M_PER_MTOK
        + Decimal(usage.cache_read_input_tokens) * CACHE_READ_PER_MTOK
        + Decimal(usage.output_tokens) * OUTPUT_PER_MTOK
    ) / million
    return value.quantize(Decimal("0.000001"))


@dataclass
class CircuitBreaker:
    retryable_streak: int = 0
    stopped_reason: str | None = None

    def record_retryable(self):
        self.retryable_streak += 1
        if self.retryable_streak >= 3:
            self.stopped_reason = "three_consecutive_retryable_failures"

    def record_success(self):
        self.retryable_streak = 0
```

`load_month_ledger`는 KST 월초를 UTC로 바꿔 `cost_usd` 합과 `submitted/failed_retryable`의 `reserved_cost_usd` 합을 읽는다. queued는 실제 호출 직전 Task 1의 원자 예약 RPC에서 cap에 편입되므로 여기서 이중 계산하지 않는다. `can_submit`은 `ledger.actual + ledger.reserved + next_reservation <= cap`일 때만 true이며 사전 표시용이고 최종 권한은 DB RPC다. `fair_selection_cap`은 slot 순서를 바깥 loop, camera id 정렬을 안쪽 loop로 순회해 남은 global night quota만큼만 돌려준다. DB error가 있으면 0으로 간주하지 않고 `BudgetUnavailable`을 던진다. 가격표 적용 종료일 이후에는 startup을 `pricing_expired`로 중단해 새 가격을 조용히 오계산하지 않는다.

- [ ] **Step 4: tests를 통과시킨다**

Run: `uv run pytest tests/test_vlm_budget.py -q`

Expected: PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_budget.py tests/test_vlm_budget.py
git commit -m "feat: Claude VLM 월 비용 가드"
```

---

### Task 8: 직접 API 전용 6-frame sampler

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_frames.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_frames.py`

**Interfaces:**
- Produces: `extract_six(video, out_dir) -> list[Path]`.

- [ ] **Step 1: 장수·중앙시각·resize/quality tests를 작성한다**

```python
def test_six_midpoint_timestamps():
    assert sample_times(30.0) == pytest.approx([2.5, 7.5, 12.5, 17.5, 22.5, 27.5])


def test_resize_never_upscales_and_caps_long_edge(tmp_path):
    small = write_image(tmp_path / "small.jpg", 320, 240)
    large = write_image(tmp_path / "large.jpg", 1920, 1080)
    assert normalize_jpeg(small) == (320, 240)
    assert normalize_jpeg(large) == (768, 432)


def test_missing_frame_is_terminal_for_sampler(tmp_path):
    with pytest.raises(FrameExtractionError, match="expected 6, got 5"):
        extract_six(Path("clip.mp4"), tmp_path, run_fn=five_frame_ffmpeg)
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_vlm_frames.py -q`

Expected: import FAIL.

- [ ] **Step 3: 기존 adaptive sampler를 수정하지 않고 새 sampler를 구현한다**

```python
def sample_times(duration):
    if not math.isfinite(duration) or duration <= 0:
        raise FrameExtractionError("video duration must be positive")
    return [(i + 0.5) * duration / 6 for i in range(6)]


def normalize_jpeg(path):
    image = cv2.imread(str(path))
    if image is None:
        raise FrameExtractionError(f"cannot decode {path.name}")
    height, width = image.shape[:2]
    if max(width, height) > 768:
        scale = 768 / max(width, height)
        image = cv2.resize(image, (round(width * scale), round(height * scale)),
                           interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    h, w = image.shape[:2]
    return w, h
```

`extract_six`는 `frames.probe_duration`을 재사용하고 ffmpeg를 각 midpoint에 실행한 뒤 정확히 6장이 아니면 실패한다. contact sheet와 bbox crop은 만들지 않는다.

- [ ] **Step 4: sampler tests를 통과시킨다**

Run: `uv run pytest tests/test_vlm_frames.py tests/test_worker.py -q`

Expected: PASS, 기존 sampler 회귀 없음.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_frames.py tests/test_vlm_frames.py
git commit -m "feat: Claude 직접 입력용 6프레임 sampler"
```

---

### Task 9: Anthropic Messages API adapter

**Files:**
- Modify: `/Users/baek/petcam-nightly-reporter/pyproject.toml`
- Modify: `/Users/baek/petcam-nightly-reporter/uv.lock`
- Create: `/Users/baek/petcam-nightly-reporter/reporter/anthropic_analyzer.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_anthropic_analyzer.py`

**Interfaces:**
- Produces: `analyze_clip(client, frame_paths, clip, model) -> AnalyzerResult`.

- [ ] **Step 1: SDK를 uv로 추가한다**

Run: `cd /Users/baek/petcam-nightly-reporter && uv add anthropic`

Expected: `pyproject.toml`과 `uv.lock`만 dependency 변경.

- [ ] **Step 2: request shape·provenance·error tests를 작성한다**

```python
def test_request_uses_six_images_schema_cache_and_exact_model(tmp_path):
    client = FakeAnthropic(response(model="claude-sonnet-5"))
    result = analyze_clip(client, six_images(tmp_path), clip(), "claude-sonnet-5")
    request = client.messages.calls[0]
    assert request["model"] == "claude-sonnet-5"
    assert len([b for b in request["messages"][0]["content"] if b["type"] == "image"]) == 6
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert result.model_actual == "claude-sonnet-5"


def test_alias_and_model_mismatch_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="exact model"):
        analyze_clip(FakeAnthropic(), six_images(tmp_path), clip(), "sonnet")
    result = analyze_clip(FakeAnthropic(response(model="claude-sonnet-5-1")),
                          six_images(tmp_path), clip(), "claude-sonnet-5")
    assert result.model_mismatch is True
```

- [ ] **Step 3: FAIL을 확인한다**

Run: `uv run pytest tests/test_anthropic_analyzer.py -q`

Expected: module import FAIL.

- [ ] **Step 4: 직접 API adapter를 구현한다**

```python
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [
            "eating_paste", "eating_prey", "drinking", "shedding",
            "moving", "unseen", "hand_feeding"
        ]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string", "maxLength": 300},
    },
    "required": ["action", "confidence", "reasoning"],
    "additionalProperties": False,
}


def analyze_clip(client, frame_paths, clip, model):
    if model == "sonnet" or not model.startswith("claude-"):
        raise ValueError("ANTHROPIC_MODEL_EXACT must be an exact model id")
    content = [_image_block(path) for path in frame_paths]
    content.append({"type": "text", "text": (
        f"clip duration={clip.duration_sec:.3f}s; frames are chronological. "
        "Classify one representative gecko behavior. Return only the schema result."
    )})
    message = client.messages.create(
        model=model,
        max_tokens=256,
        temperature=0,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        metadata={"user_id": hashlib.sha256(clip.id.encode()).hexdigest()},
    )
    text = next(block.text for block in message.content if block.type == "text")
    payload = json.loads(text)
    usage = Usage(
        input_tokens=message.usage.input_tokens,
        cache_creation_input_tokens=message.usage.cache_creation_input_tokens or 0,
        cache_read_input_tokens=message.usage.cache_read_input_tokens or 0,
        output_tokens=message.usage.output_tokens,
    )
    return AnalyzerResult(message.id, model, message.model, payload, usage,
                          calculate_cost(usage), message.model != model)
```

같은 파일에 다음 반환 타입을 정의한다.

```python
@dataclass(frozen=True, slots=True)
class AnalyzerResult:
    provider_request_id: str
    model_requested: str
    model_actual: str
    result: dict[str, object]
    usage: Usage
    cost_usd: Decimal
    model_mismatch: bool
```

`_image_block`은 JPEG bytes를 base64로 인코딩하되 반환 뒤 bytes를 DB/log에 남기지 않는다. Anthropic `RateLimitError`, `APIConnectionError`, `InternalServerError`는 retryable, `AuthenticationError`, `PermissionDeniedError`, `BadRequestError`는 terminal code로 변환한다. response에 text/usage/schema 필드가 없으면 `invalid_provider_response` terminal이다.

- [ ] **Step 5: adapter tests와 전체 tests를 통과시킨다**

Run: `uv run pytest tests/test_anthropic_analyzer.py -q && uv run pytest -q`

Expected: 모두 PASS. 실제 API는 호출하지 않는다.

- [ ] **Step 6: 커밋한다**

```bash
git add pyproject.toml uv.lock reporter/anthropic_analyzer.py tests/test_anthropic_analyzer.py
git commit -m "feat: Anthropic 직접 이미지 VLM adapter"
```

---

### Task 10: Selection + analysis worker orchestration

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_candidate_worker.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_candidate_worker.py`

**Interfaces:**
- Produces: `run(now=None, sb=None, analyzer_client=None) -> int`, `process_jobs(...) -> dict`.

- [ ] **Step 1: durable-before-cost·격리·shadow tests를 작성한다**

```python
def test_api_is_never_called_when_run_job_rpc_fails():
    analyzer = SpyAnalyzer()
    with pytest.raises(RuntimeError):
        run_once(sb=FailingRpcSB(), analyzer_fn=analyzer, now=TRIGGER)
    assert analyzer.calls == []


def test_one_clip_failure_does_not_block_other_slots_and_temp_is_clean(tmp_path):
    stats = process_jobs(sb(), four_jobs(), analyzer_fn=fail_first_then_succeed,
                         download_fn=fake_download, frames_fn=fake_frames, temp_root=tmp_path)
    assert stats == {"succeeded": 3, "failed_retryable": 1, "held": 0}
    assert list(tmp_path.iterdir()) == []


def test_worker_never_writes_product_tables():
    fake = FakeSB(seed())
    run_once(sb=fake, analyzer_fn=fake_analyzer, now=TRIGGER)
    assert "behavior_logs" not in fake.store
    assert "camera_clips" not in fake.store
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_vlm_candidate_worker.py -q`

Expected: module import FAIL.

- [ ] **Step 3: worker를 구현한다**

```python
def run(*, now=None, sb=None, analyzer_client=None):
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))
    if not config.VLM_ROUTER_ENABLED:
        print("[vlm-router] disabled — selection/API skipped", flush=True)
        return 0
    start, end = trigger_window(now)
    lock = _acquire_lock()
    if lock is None:
        print("[vlm-router] already running — skip", flush=True)
        return 0
    try:
        sb = sb or create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        ledger = load_month_ledger(sb, now)
        clips = load_window_candidates(sb, start, end,
                                       config.VLM_ACTIVITY_POLICY_VERSION,
                                       config.VLM_SELECTOR_VERSION)
        contexts = {}
        selected_by_camera = {}
        for camera_id, camera_clips in groupby_camera(clips):
            eligible, reasons = partition_eligibility(camera_clips)
            reps = reduce_episodes(eligible, start)
            contexts[camera_id] = (camera_clips, reps, reasons)
            selected_by_camera[camera_id] = select_candidates(
                reps,
                load_recent_selection_history(sb, camera_id, now - timedelta(days=7)),
                start,
            )
        night_start, night_end = kst_night_bounds(end)
        remaining = max(0, 64 - load_night_job_count(sb, night_start, night_end))
        globally_capped = fair_selection_cap(selected_by_camera, remaining)
        for camera_id, (camera_clips, reps, reasons) in contexts.items():
            selected = globally_capped.get(camera_id, [])
            if load_camera_night_job_count(sb, camera_id, night_start, night_end) >= 16:
                selected = []
            create_run_and_jobs(sb, build_run(camera_id, start, end, camera_clips,
                                               reps, selected, reasons, ledger),
                                build_jobs(selected))
        jobs = fair_job_order(load_due_jobs(sb, now, limit=64))
        stats = process_jobs(sb, jobs, analyzer_client=analyzer_client)
        post_owner_summary(stats)
        return 0
    finally:
        _release_lock(lock)
```

`groupby_camera`는 `camera_id`를 정렬한 `dict[str, list[CandidateClip]]`을 반환한다. `contexts`는 camera별 `clips/reps/reasons`를 보존해 두 번째 loop에서 다른 카메라 값을 재사용하지 않는다. `kst_night_bounds`는 현재 trigger가 속한 KST 20:00~다음날 04:00을 UTC로 반환한다. `load_night_job_count`와 `load_camera_night_job_count`는 DB error 시 cap을 0으로 완화하지 않고 run 전체를 중단한다. `build_run`은 Task 1 RPC의 p_run 17개 필드를 모두 채우고, `build_jobs`는 각 `SelectedCandidate`를 Task 1의 p_jobs 필드로 직렬화하면서 `reserved_cost_usd=0.10`, exact model/prompt/sampler/pricing version을 넣는다. `$0.10`은 실제 예상비용이 아니라 한 요청이 월 hard cap을 넘지 않게 잡는 보수적 예약 상한이며 성공 후 실제 usage 비용으로 교체된다. 두 helper는 각각 `dict[str, object]`, `list[dict[str, object]]`를 반환하며 누락 필드가 있으면 RPC 전에 `ValueError`를 던진다.

`process_jobs`의 순서는 job별 `can_submit` → `mark_submitted` → R2 download → `extract_six` → Messages API → `mark_result`다. budget hold는 API 전에 `held_budget`; model mismatch는 결과/usage를 저장한 뒤 `held_model_mismatch`이고 즉시 breaker stop이다. API 성공 후 `mark_result` 실패 시 provider request id만 stderr에 남기고 해당 job을 다시 호출하지 않는다. 한 clip의 R2/decode 오류는 최대 attempt 2까지 retryable로 저장한다. 모든 mp4/JPEG는 `TemporaryDirectory`와 per-clip `unlink`로 정리한다.

- [ ] **Step 4: worker tests와 전체 nightly tests를 실행한다**

Run: `uv run pytest tests/test_vlm_candidate_worker.py -q && uv run pytest -q`

Expected: 모두 PASS, 기존 activity worker tests도 PASS.

- [ ] **Step 5: 커밋한다**

```bash
git add reporter/vlm_candidate_worker.py tests/test_vlm_candidate_worker.py
git commit -m "feat: 예산 고정형 VLM 후보 worker"
```

---

### Task 11: Offline replay와 사람 GT 30개 평가 도구

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/scripts/replay_vlm_selector.py`
- Create: `/Users/baek/petcam-nightly-reporter/scripts/select_vlm_gt30.py`
- Create: `/Users/baek/petcam-nightly-reporter/scripts/eval_vlm_direct_api.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_eval_scripts.py`
- Modify: `/Users/baek/petcam-nightly-reporter/.gitignore`

**Interfaces:**
- Outputs local-only CSV/JSON under `/Users/baek/petcam-nightly-reporter/storage/vlm-eval/`.

- [ ] **Step 1: replay/manifest validation tests를 작성한다**

```python
def test_manifest_requires_exactly_30_unique_human_labels(tmp_path):
    path = tmp_path / "gt30.csv"
    write_manifest(path, 29)
    with pytest.raises(ValueError, match="exactly 30"):
        validate_manifest(path)


def test_quality_gate_thresholds():
    report = evaluate_metrics(direct_rows(), cli_rows(), gt_rows())
    assert report["median_input_tokens"] <= 12000
    assert report["p95_input_tokens"] <= 20000
    assert report["accuracy_delta_pp"] >= -3
    assert report["critical_fn_delta"] <= 1
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_vlm_eval_scripts.py -q`

Expected: import FAIL.

- [ ] **Step 3: 세 script를 구현한다**

`replay_vlm_selector.py`는 `--window-end`를 세 번 받아 API 없이 selected clip/slot/reason을 JSON으로 출력하고 다음 assertion 실패 시 exit 2로 끝낸다.

```python
assert len({(row["camera_id"], row["window_start"], row["slot"]) for row in rows}) == len(rows)
assert all(count <= 4 for count in jobs_per_camera_window(rows).values())
assert len(rows) <= 64
assert no_episode_occupies_two_slots(rows)
```

`select_vlm_gt30.py`는 `behavior_labels`의 7개 호환 action, 같은 clip의 기존 `behavior_logs(source='vlm')` v4.0 baseline, R2가 모두 있는 clip을 action/time/motion bucket별 deterministic stratified sampling해 후보 30개를 만든다. owner가 각 영상을 다시 보고 `human_action`을 확정하기 전 `approved=false`로 기록한다. `eval_vlm_direct_api.py`는 `approved=true` 30개만 허용하고 `--execute-paid`가 없으면 request 예상치만 출력한다.

```python
if args.execute_paid is False:
    print(json.dumps({"clips": 30, "estimated_reserved_usd": "3.00", "api_calls": 0}))
    return 0
if any(row["approved"].lower() != "true" for row in manifest):
    raise ValueError("all 30 GT rows require owner approved=true")
```

`.gitignore`에 `/storage/`를 추가해 clip UUID manifest, downloaded mp4, API 결과 원문이 커밋되지 않게 한다.

- [ ] **Step 4: tests와 3-window 무료 replay를 실행한다**

Run: `uv run pytest tests/test_vlm_eval_scripts.py -q`

Run: `uv run python scripts/replay_vlm_selector.py --window-end 2026-07-12T22:00:00+09:00 --window-end 2026-07-13T00:00:00+09:00 --window-end 2026-07-13T02:00:00+09:00`

Expected: API calls `0`, camera/window cap 위반 `0`, episode 중복 `0`, absent/static hard skip `0`.

- [ ] **Step 5: 커밋한다**

```bash
git add .gitignore scripts/replay_vlm_selector.py scripts/select_vlm_gt30.py scripts/eval_vlm_direct_api.py tests/test_vlm_eval_scripts.py
git commit -m "test: VLM selector replay와 GT30 평가 도구"
```

---

### Task 12: 별도 launchd installer와 shadow-safe 환경

**Files:**
- Create: `/Users/baek/petcam-nightly-reporter/install-launchd-vlm-candidate.sh`
- Modify: `/Users/baek/petcam-nightly-reporter/.env.example`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_install_vlm_launchd.py`

**Interfaces:**
- Produces LaunchAgent label `com.petcam.vlm-candidate-worker`.

- [ ] **Step 1: plist 계약 test를 작성한다**

```python
def test_installer_has_four_exact_triggers_and_shadow_guards():
    text = Path("install-launchd-vlm-candidate.sh").read_text()
    assert text.count("<key>Hour</key>") == 4
    assert all(f"<integer>{hour}</integer>" in text for hour in (22, 0, 2, 4))
    assert "REGISTER_HIGHLIGHTS" in text and "<string>0</string>" in text
    assert "RunAtLoad" not in text
    assert "plutil -lint" in text
```

- [ ] **Step 2: FAIL을 확인한다**

Run: `uv run pytest tests/test_install_vlm_launchd.py -q`

Expected: installer file missing으로 FAIL.

- [ ] **Step 3: disabled-by-default installer를 작성한다**

```bash
LABEL="com.petcam.vlm-candidate-worker"
ENABLE="${VLM_ROUTER_ENABLED:-0}"
if [[ "$ENABLE" != "0" && "$ENABLE" != "1" ]]; then
  echo "VLM_ROUTER_ENABLED must be 0 or 1" >&2
  exit 1
fi
```

plist의 `ProgramArguments`는 `uv run python -m reporter.vlm_candidate_worker`, `StartCalendarInterval`은 Hour 22/0/2/4와 Minute 0의 dict 네 개다. EnvironmentVariables에는 `PATH`, `VLM_ROUTER_ENABLED=$ENABLE`, `REGISTER_HIGHLIGHTS=0`, `ANTHROPIC_MODEL_EXACT=claude-sonnet-5`만 넣는다. API key는 plist에 넣지 않고 repo `.env`에서 읽는다. `RunAtLoad`는 넣지 않는다. 설치 전 `plutil -lint`가 실패하면 bootstrap하지 않는다.

`.env.example`에는 다음을 추가한다.

```dotenv
ANTHROPIC_API_KEY=your-anthropic-api-key
ANTHROPIC_MODEL_EXACT=claude-sonnet-5
VLM_ROUTER_ENABLED=0
VLM_MONTHLY_BUDGET_USD=10.00
VLM_RESERVED_COST_USD=0.10
```

- [ ] **Step 4: shell/test 검증만 하고 실제 설치는 하지 않는다**

Run: `bash -n install-launchd-vlm-candidate.sh && uv run pytest tests/test_install_vlm_launchd.py -q`

Expected: PASS. `launchctl bootstrap`은 이 단계에서 실행하지 않는다.

- [ ] **Step 5: 커밋한다**

```bash
git add install-launchd-vlm-candidate.sh .env.example tests/test_install_vlm_launchd.py
git commit -m "feat: VLM 후보 worker 고정 스케줄 설치기"
```

---

### Task 13: 전체 검증, 코드 리뷰, main push

**Files:** 모든 구현 파일. DB/유료 호출/launchd write 없음.

- [ ] **Step 1: 전체 검증을 실행한다**

Run: `cd /Users/baek/petcam-nightly-reporter && uv run pytest -q`

Expected: 전체 PASS.

Run: `cd /Users/baek/petcam-lab && uv run pytest -q`

Expected: 기존 334개 + migration contract 2개 PASS.

Run: `git -C /Users/baek/petcam-nightly-reporter diff --check && git -C /Users/baek/petcam-lab diff --check`

Expected: 출력 없음.

- [ ] **Step 2: 금지 경로를 정적 검사한다**

Run: `rg -n "behavior_logs|register_highlight|camera_clips.*insert|delete_clip" reporter/vlm_* reporter/anthropic_analyzer.py`

Expected: 결과 0건.

Run: `rg -n "model=.sonnet.|ANTHROPIC_MODEL_EXACT=.sonnet." reporter install-launchd-vlm-candidate.sh .env.example`

Expected: alias 사용 0건.

- [ ] **Step 3: 사용자에게 diff·test·예상 유료 상한을 보고하고 통합 승인을 받는다**

보고에는 두 레포 commit 목록, tests 수, API call 0, DB write 0, launchd 미설치, 월 cap `$10`, 30개 평가 예약 상한 `$3.00`을 포함한다.

- [ ] **Step 4: 승인 뒤 두 레포 main을 non-force push한다**

```bash
git -C /Users/baek/petcam-lab push origin main
git -C /Users/baek/petcam-nightly-reporter push origin main
```

Expected: local main == origin/main, force push 없음.

---

### Task 14: Production migration 적용 checkpoint

**Files:** code change 없음.

- [ ] **Step 1: 사용자에게 production DB schema 적용 승인을 별도로 받는다**

승인 전에는 다음 명령 또는 Supabase migration 도구를 실행하지 않는다.

- [ ] **Step 2: 승인 후 forward migration을 적용한다**

Apply exact file: `/Users/baek/petcam-lab/migrations/2026-07-15_clip_vlm_candidate_jobs.sql`.

- [ ] **Step 3: read-only/rollback probe를 실행한다**

```sql
select count(*) from public.clip_vlm_selector_runs; -- 0
select count(*) from public.clip_vlm_jobs;          -- 0
select policyname, cmd from pg_policies
 where schemaname='public' and tablename in ('clip_vlm_selector_runs','clip_vlm_jobs')
 order by tablename, policyname;                    -- owner SELECT 1개씩
select routine_name from information_schema.routines
 where routine_schema='public' and routine_name='fn_create_clip_vlm_selector_run'; -- 1
```

transaction probe는 존재하는 camera/clip을 subquery로 고르고, run+최대4 jobs 생성 뒤 count/unique/RLS를 검사하고 전체 `ROLLBACK`한다. 잔류 run/job이 0인지 다시 확인한다.

- [ ] **Step 4: schema 적용 결과만 SOT에 기록하고 멈춘다**

이 단계에서도 API·launchd·behavior_logs write는 0이다.

---

### Task 15: 사람 승인 GT30 direct API canary checkpoint

**Files:** local `storage/vlm-eval/`만 생성, commit 금지.

- [ ] **Step 1: 후보 30개를 생성하고 owner가 영상을 재검수한다**

Run: `cd /Users/baek/petcam-nightly-reporter && uv run python scripts/select_vlm_gt30.py --output storage/vlm-eval/gt30.csv`

Expected: 30 unique clip, 7-class 호환, `approved=false`.

- [ ] **Step 2: owner가 human_action을 확인해 30개 모두 approved=true로 만든 뒤 validator를 실행한다**

Run: `uv run python scripts/eval_vlm_direct_api.py --manifest storage/vlm-eval/gt30.csv`

Expected: `api_calls=0`, `estimated_reserved_usd=3.00`.

- [ ] **Step 3: 사용자에게 최대 `$3.00` 유료 호출 승인을 별도로 받는다**

승인 없이 `--execute-paid`를 쓰지 않는다.

- [ ] **Step 4: 승인 뒤 direct API 30건을 1회 실행한다**

Run: `uv run python scripts/eval_vlm_direct_api.py --manifest storage/vlm-eval/gt30.csv --execute-paid`

통과 기준:

- median total input token `<= 12,000/clip`
- p95 total input token `<= 20,000/clip`
- 기존 CLI exact action accuracy 대비 하락 `<= 3%p`
- drinking/feeding/shedding false negative 증가 `<= 1건`
- model/prompt/sampler/usage provenance 30/30

하나라도 실패하면 launchd 유료 shadow를 활성화하지 않는다.

---

### Task 16: 3일 paid shadow와 최종 유지 판단

**Files:**
- Modify after observation: `/Users/baek/petcam-lab/docs/DATABASE.md`
- Modify after observation: `/Users/baek/petcam-lab/specs/next-session.md`
- Modify after observation: `/Users/baek/petcam-lab/.claude/donts-audit.md`

- [ ] **Step 1: 기존 Claude CLI worker를 중지하되 activity worker는 유지한다**

```bash
launchctl bootout "gui/$(id -u)/com.petcam.nightly-reporter" 2>/dev/null || true
launchctl print "gui/$(id -u)/com.petcam.activity-worker"
```

Expected: old Claude CLI job 없음, activity worker 존재.

- [ ] **Step 2: 사용자 승인 뒤 paid shadow LaunchAgent를 설치한다**

Run: `cd /Users/baek/petcam-nightly-reporter && VLM_ROUTER_ENABLED=1 ./install-launchd-vlm-candidate.sh`

Expected: 네 trigger, `REGISTER_HIGHLIGHTS=0`, model exact, RunAtLoad 없음.

- [ ] **Step 3: 첫 scheduled window 뒤 안전 조건을 확인한다**

- run은 카메라당 1개, jobs는 카메라당 0~4개.
- `behavior_logs` count 변화 0.
- `clip_vlm_jobs` 이외 제품 테이블 write 0.
- model requested/actual 일치.
- cost/usage 누락 0.
- temp mp4/jpeg 잔존 0.
- 전체 밤 64 job 이하.

하나라도 위반하면 즉시 다음을 실행한다.

```bash
launchctl bootout "gui/$(id -u)/com.petcam.vlm-candidate-worker"
```

- [ ] **Step 4: 3개 날짜 동안 owner blind audit를 수행한다**

매일 네 슬롯 결과와 무선택 일반 pool 4개를 사람이 본다. 중요한 행동·고객가치 clip이 무선택 표본에서 1건이라도 나오면 자동 승격을 금지하고 selector 원인을 수정한다. absent/static false exclusion은 Gate v3 hardcase로 기록하되 detector GT로 자동 사용하지 않는다.

- [ ] **Step 5: 월 projected cost와 품질을 보고해 유지·수정·중단 결정을 받는다**

projected monthly cost가 `$10`을 넘으면 후보 수를 늘리지 않는다. 먼저 `diversity_discovery`와 `exclusion_audit`만 Batch API로 옮기는 별도 설계를 작성한다. 고객 노출/behavior_logs 승격은 이 계획 범위에서 실행하지 않는다.

- [ ] **Step 6: 실제 상태를 SOT에 기록하고 문서 commit/push 승인을 받는다**

```bash
git add docs/DATABASE.md specs/next-session.md .claude/donts-audit.md
git commit -m "docs: 예산 고정형 VLM shadow 결과 정리"
git push origin main
```

---

## Final Acceptance Checklist

- [ ] 같은 입력·selector version의 offline replay가 같은 clip/slot을 고른다.
- [ ] 같은 episode가 두 슬롯을 점유하지 않는다.
- [ ] Gate absent/static/unknown은 hard skip되지 않는다.
- [ ] camera/window 4, camera/night 16, global/night 64 상한이 지켜진다.
- [ ] run/job은 API 호출 전에 원자적으로 durable하다.
- [ ] 월 cap/원장 실패가 fail-closed로 동작한다.
- [ ] 모델 alias가 없고 requested/actual mismatch가 breaker를 연다.
- [ ] 직접 API 30개 품질/token gate를 통과한다.
- [ ] 3일 shadow 동안 product table write와 고객 노출이 0이다.
- [ ] Gate v3 hardcase provenance가 보존되고 product exclude와 presence GT가 섞이지 않는다.
- [ ] 기존 activity worker와 Flutter effective activity canary가 영향받지 않는다.

## Implementation References

- Anthropic Python Messages API: <https://platform.claude.com/docs/en/api/python/messages/create>
- Structured JSON output (`output_config.format`): <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>
- Prompt caching과 usage 필드: <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
