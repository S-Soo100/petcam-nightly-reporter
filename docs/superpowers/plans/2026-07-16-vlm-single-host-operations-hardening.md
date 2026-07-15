# VLM Single-Host Operations Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Do not execute the superseded 2026-07-15 reliability plan separately.

**Goal:** 카메라별 최대 4개의 정규 VLM 후보 분석을 Mac mini 단일 host로 이전하고, 정규/backfill queue를 격리하며, Claude CLI 실패를 안전하게 진단하고, scheduled run마다 VLM 전용 Slack 결과를 남긴다.

**Architecture:** 정규 worker는 fail-closed host guard를 통과한 뒤 현재 selector/window job만 생성·처리하고, 오래된 정규 recovery는 현재 window 뒤에 bounded 처리한다. backfill worker는 backfill selector만 소유한다. CLI adapter는 redacted diagnostic과 최대 1회 subretry를 제공하고, worker는 terminal 집계 후 Slack 요약을 한 번 보낸다.

**Tech Stack:** Python 3.12, pytest, Supabase/PostgreSQL, Claude Code CLI subscription, launchd, Slack webhook, uv.

## 0. 실행 계약

- 설계 정본: `/Users/baek/petcam-nightly-reporter/specs/2026-07-16-vlm-single-host-operations-hardening-design.md`
- 이 plan은 다음 미커밋 초안을 통합 대체한다. 파일은 보존하되 따로 실행하지 않는다.
  - `docs/superpowers/plans/2026-07-15-claude-cli-batch-reliability-hardening.md`
  - `specs/2026-07-15-claude-cli-batch-reliability-hardening-design.md`
- TDD를 지킨다. 각 기능은 실패 테스트 → 최소 구현 → 관련 테스트 통과 순서다.
- 변경 파일 10개 이상이므로 `git diff --stat` → 기능 그룹별 검토 순서를 지킨다.
- 기존 `.env.bak-*`, `storage/`, untracked 영상은 add/edit/delete하지 않는다.
- provider=`claude_cli_batch`, exact model=`claude-sonnet-5`, clip당 6 frame, camera·window당 최대 4개, night 전체 최대 64개를 유지한다.
- `REGISTER_HIGHLIGHTS=0`, 직접 API 비활성, GT/app/activity/Gate 쓰기 금지다.
- production migration apply, commit/push, LaunchAgent bootout/bootstrap, 실제 Claude canary, DB settings 변경은 별도 사용자 승인 전 금지한다.
- 범위 밖 refactor·format churn 금지다.

## 1. 시작 전 사실 고정

**Files:** read-only

- `/Users/baek/petcam-nightly-reporter/reporter/vlm_candidate_worker.py`
- `/Users/baek/petcam-nightly-reporter/reporter/vlm_store.py`
- `/Users/baek/petcam-nightly-reporter/reporter/vlm_backfill_worker.py`
- `/Users/baek/petcam-nightly-reporter/reporter/claude_cli_analyzer.py`
- `/Users/baek/petcam-nightly-reporter/reporter/worker.py`
- `/Users/baek/petcam-nightly-reporter/install-launchd-vlm-candidate.sh`
- `/Users/baek/petcam-nightly-reporter/tests/test_vlm_worker.py`
- `/Users/baek/petcam-nightly-reporter/tests/test_vlm_runtime.py`
- `/Users/baek/petcam-nightly-reporter/tests/test_vlm_backfill_worker.py`
- `/Users/baek/petcam-nightly-reporter/tests/test_claude_cli_analyzer.py`
- `/Users/baek/petcam-nightly-reporter/tests/test_install_vlm_launchd.py`

- [ ] **Step 1: baseline과 작업트리 기록**

```bash
cd /Users/baek/petcam-nightly-reporter
git status --short
git branch --show-current
git rev-parse HEAD
git diff --stat
uv run pytest tests/test_vlm_worker.py tests/test_vlm_runtime.py tests/test_vlm_backfill_worker.py tests/test_claude_cli_analyzer.py tests/test_install_vlm_launchd.py -q
```

Expected: 관련 baseline tests green. 기존 미커밋 두 문서는 사용자 작업으로 기록하고 수정하지 않는다.

- [ ] **Step 2: 금지 write 경로 검색**

```bash
rg -n "behavior_labels|behavior_logs|REGISTER_HIGHLIGHTS|effective_activity|camera_activity_filter|direct_api" reporter/vlm_* reporter/claude_cli_analyzer.py install-launchd-vlm-candidate.sh
```

Expected: 기존 의도된 direct API compatibility 외 새 write 경로 없음. 새 구현에서 금지 테이블 write를 추가하지 않는다.

- [ ] **Step 3: baseline이 실패하면 중단**

기존 실패를 구현에 섞지 않는다. 실패 test, traceback, 현재 HEAD를 보고하고 사용자 판단을 기다린다.

---

## 2. Queue ownership을 selector/window로 격리

**Files:**

- Modify: `/Users/baek/petcam-nightly-reporter/reporter/vlm_store.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/vlm_candidate_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/vlm_backfill_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/install-launchd-vlm-backfill.sh`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_runtime.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_backfill_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_install_vlm_backfill.py`

**Interfaces:**

- Add: `load_due_jobs_for_selector_window(sb, selector_version, start, end, limit=4)`
- Add: `load_recovery_jobs_for_selector(sb, selector_version, before, limit=4)`
- Remove runtime use: candidate worker의 global `load_due_jobs()`
- Remove: backfill worker의 regular-selector drain branch

- [ ] **Step 1: store query RED tests 작성**

다음 fixture를 섞는다.

- current regular queued
- current regular failed_retryable
- old regular failed_retryable
- current backfill queued
- old backfill failed_retryable
- regular succeeded
- regular failed_terminal
- held_model_mismatch

Assertions:

1. current loader는 같은 selector와 `[start,end)`의 queued/retryable만 반환한다.
2. recovery loader는 같은 selector, `window_start < current_start`, queued/retryable만 반환한다.
3. 두 loader 모두 limit=4를 지킨다.
4. succeeded/terminal/held는 반환하지 않는다.
5. queued와 retryable을 합친 뒤 `queued_at` 오름차순으로 안정 정렬한다. status별 query append 순서에 의존하지 않는다.

Run:

```bash
uv run pytest tests/test_vlm_runtime.py -q
```

Expected: 새 함수가 없어 RED.

- [ ] **Step 2: selector/window loader 최소 구현**

주의:

- Supabase query의 status별 결과를 합친 뒤 Python에서 `queued_at`을 parse/정렬한다.
- 각 status query에 limit를 적용하더라도 최종 slice가 global limit를 지켜야 한다.
- `start` inclusive, `end` exclusive다.
- `before`는 current `window_start` exclusive다.

- [ ] **Step 3: candidate current-first RED tests 작성**

`run()` dependency seam을 필요한 최소 범위로 추가해 다음을 검증한다.

1. 현재 window job이 있으면 그것만 첫 `process_fn`에 전달된다.
2. current 처리 후 `queued|failed_retryable`가 남으면 recovery를 실행하지 않는다.
3. current due가 0개로 재확인된 뒤 recovery를 하더라도 정규 selector old job 최대 4개뿐이다.
4. backfill job은 current/recovery 어느 호출에도 들어가지 않는다.
5. current processing이 auth/quota/model breaker를 반환하면 recovery를 실행하지 않는다.
6. current job이 없더라도 run/job row 생성과 Slack 요약 경로는 유지된다.
7. 동일 window 재실행에서 create RPC가 idempotent하면 중복 job 없이 기존 current due만 처리한다.

Expected: 현재 global `load_due_jobs()` 때문에 RED.

- [ ] **Step 4: candidate worker current-first 구현**

권장 구조:

```python
current_due = load_due_jobs_for_selector_window(
    sb, config.VLM_SELECTOR_VERSION, start, end,
    limit=config.VLM_MAX_PER_NIGHT,
)
current_stats = process_fn(sb, current_due)

current_remaining = load_due_jobs_for_selector_window(
    sb, config.VLM_SELECTOR_VERSION, start, end,
    limit=1,
)
if not breaker_triggered(current_stats) and not current_remaining:
    recovery_due = load_recovery_jobs_for_selector(
        sb, config.VLM_SELECTOR_VERSION, before=start,
        limit=config.VLM_MAX_PER_CAMERA_WINDOW,
    )
    recovery_stats = process_fn(sb, recovery_due)
```

`breaker_triggered`는 string 추측이 아니라 process result의 명시 field를 사용하도록 Task 5에서 완성한다. Task 2에서는 dependency seam 또는 임시 명시 result type으로 컴파일 가능하게 유지한다.

- [ ] **Step 5: backfill isolation RED test 작성**

기존 `test_vlm_backfill_worker.py`의 regular-first 기대를 삭제하지 말고 새 계약으로 교체한다.

Assertions:

- regular job만 있어도 backfill worker가 `process_fn`으로 넘기지 않는다.
- existing backfill wave가 있으면 해당 selector/window job만 처리한다.
- 신규 wave도 backfill selector job만 처리한다.
- regular queued 수는 backfill run 전후 불변이다.

- [ ] **Step 6: backfill regular drain 제거**

다음 블록을 제거한다.

```python
regular = load_due_jobs_for_selector(sb, config.VLM_SELECTOR_VERSION, None, None)
if regular:
    process_fn(sb, regular)
    return 0
```

backfill worker는 `BACKFILL_SELECTOR_VERSION` 외 selector를 인자로 받는 경로가 없어야 한다.

- [ ] **Step 7: backfill daytime guard RED tests 작성**

`backfill_allowed_now(now)` pure helper와 `run()` ordering을 검증한다.

- KST 07:00, 12:00, 19:59 → allowed
- KST 20:00, 22:00, 00:00, 04:00, 06:59 → denied
- denied일 때 lock/DB/R2/Gate/Claude call 0
- UTC input도 KST로 변환해 판정

- [ ] **Step 8: backfill daytime guard 구현**

guard는 lock과 Supabase client보다 먼저 실행한다. production 범위는 `07:00 <= KST local time < 20:00`으로 고정한다. 임의 env로 야간 허용 범위를 늘리지 않는다.

- [ ] **Step 9: backfill installer RED tests 작성**

기존 hourly/RunAtLoad 기대를 새 안전 계약으로 바꾼다.

- `RunAtLoad` 없음
- `StartInterval` 없음
- `StartCalendarInterval`에 07~19시 정각만 존재
- 20·21·22·23·00·02·04시 entry 없음
- provider/model/shadow-safe env 회귀 유지

- [ ] **Step 10: backfill installer calendar schedule 구현**

명시 `<array>` calendar entries를 생성하고 `plutil -lint`를 bootstrap 전에 유지한다. 설치 출력도 `07:00~19:00 KST hourly`로 정정한다.

- [ ] **Step 11: queue isolation 검증**

```bash
uv run pytest tests/test_vlm_runtime.py tests/test_vlm_worker.py tests/test_vlm_backfill_worker.py tests/test_install_vlm_backfill.py -q
rg -n "load_due_jobs\(" reporter tests
rg -n "VLM_SELECTOR_VERSION" reporter/vlm_backfill_worker.py
```

Expected:

- tests green
- production candidate runtime에서 global loader 사용 0
- backfill worker에서 regular selector drain 0

---

## 3. Fail-closed production host guard

**Files:**

- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_host_guard.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_host_guard.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/config.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/vlm_candidate_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/.env.example`

**Interfaces:**

- Add: `require_expected_host(actual: str, expected: str | None) -> None`
- Add config: `VLM_EXPECTED_HOST`
- Add exception: `HostOwnershipError` with safe actual/expected host labels only

- [ ] **Step 1: host guard RED tests 작성**

Cases:

- exact hostname match → pass
- expected missing/blank → fail
- MacBook actual vs Mac mini expected → fail
- whitespace around expected → normalize then exact compare
- short name vs FQDN은 자동 동치 처리하지 않는다. 운영 hostname을 정확히 기록한다.

- [ ] **Step 2: pure guard 구현**

host string에는 secret이 없지만 newline/control characters를 로그에 넣지 않도록 printable subset으로 normalize한다.

- [ ] **Step 3: run ordering RED test 작성**

host mismatch에서 다음 dependency call count가 모두 0인지 검증한다.

- `create_client`
- `load_window_candidates`
- `create_run_and_jobs`
- `process_cli_jobs`
- Slack send

- [ ] **Step 4: worker 최상단에 guard 적용**

`enabled` 확인 다음, `trigger_window`·lock·DB 전에 guard한다. disabled worker는 host 설정이 없어도 기존처럼 정상 no-op할 수 있다. enabled production worker만 fail-closed다.

- [ ] **Step 5: env 문서화**

`.env.example`에는 실제 Mac mini hostname을 하드코딩하지 않는다.

```dotenv
# Production candidate worker required. Must equal the verified Mac mini hostname.
VLM_EXPECTED_HOST=
```

- [ ] **Step 6: 검증**

```bash
uv run pytest tests/test_vlm_host_guard.py tests/test_vlm_worker.py -q
```

---

## 4. Safe `cli_rc_1` diagnostic용 forward migration

**Files:**

- Create: `/Users/baek/petcam-lab/migrations/2026-07-16_clip_vlm_failure_diagnostic.sql`

**Produces:** nullable `public.clip_vlm_jobs.failure_diagnostic jsonb`

- [ ] **Step 1: production schema read-only 확인**

Supabase 연결이 사용 가능할 때 read-only로만 실행한다.

```sql
select column_name, data_type
from information_schema.columns
where table_schema='public' and table_name='clip_vlm_jobs'
order by ordinal_position;

select status, count(*)
from public.clip_vlm_jobs
group by status
order by status;
```

Expected: baseline count 기록. update/insert 금지.

- [ ] **Step 2: idempotent forward migration 작성**

기존 7월 15일 migration 파일은 만들거나 수정하지 않는다. 현재 날짜 파일 하나를 정본으로 만든다.

Requirements:

- `add column if not exists failure_diagnostic jsonb`
- object-or-null CHECK
- comment에 raw stdout/stderr 금지 명시
- 기존 RLS/grant 불변
- rollback SQL은 파일 하단 comment로 제공
- constraint는 `DO $$`로 존재 확인 후 추가해 재실행 가능하게 한다.

- [ ] **Step 3: static 검증 후 중단**

```bash
cd /Users/baek/petcam-lab
git diff --check -- migrations/2026-07-16_clip_vlm_failure_diagnostic.sql
rg -n "stdout|stderr|token|email" migrations/2026-07-16_clip_vlm_failure_diagnostic.sql
```

Expected: migration 작성만. apply 금지.

---

## 5. Claude CLI diagnostic, retry matrix, breaker

**Files:**

- Modify: `/Users/baek/petcam-nightly-reporter/reporter/claude_cli_analyzer.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_claude_cli_analyzer.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/vlm_candidate_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/vlm_store.py`

**Interfaces:**

- Add dataclass: `CliFailureDiagnostic`
- Extend: `CliBatchError(code, diagnostic, disposition)`
- Add: `analyze_batch_with_retry(...)`
- Add process result dataclass: stats + breaker + diagnostics

- [ ] **Step 1: redaction/fingerprint RED tests 작성**

Fixture raw output에 모두 포함한다.

- email
- bearer/token-looking string
- `/Users/name/...` path
- full UUID
- ISO timestamp
- ANSI control sequences
- CLI stderr message

Assertions:

- diagnostic JSON과 exception `str()`에 원문이 없다.
- fingerprint는 동일 의미/가변 UUID·path·timestamp에서 안정적이다.
- allowlist 밖 marker는 저장하지 않는다.
- stdout/stderr는 byte count만 보존한다.

- [ ] **Step 2: phase 분류 RED tests 작성**

Cases:

- auth preflight failure → `auth`, breaker
- executable missing/permission → `spawn`, breaker
- timeout/transient network → `process`, retryable
- unknown rc=1 → `process`, one-subretry
- envelope JSON failure → `envelope`, no immediate retry
- VLM schema failure → `schema`, no immediate retry
- clip set mismatch → `clip_set`, breaker
- exact model mismatch → `model`, breaker
- quota marker → `process` 또는 전용 code, breaker

- [ ] **Step 3: safe diagnostic 최소 구현**

허용 schema는 설계 §8을 그대로 따른다. raw stdout/stderr를 attribute로 유지하더라도 DB/log-facing serialization에서는 접근할 수 없게 분리한다. 가능하면 process local 변수 밖으로 원문을 전달하지 않는다.

- [ ] **Step 4: subattempt RED tests 작성**

1. 첫 호출 성공 → 1회, diagnostic null
2. transient 실패 후 성공 → 2회, recovered=true
3. unknown rc1 후 성공 → 2회, recovered=true
4. transient 두 번 실패 → 2회, recovered=false
5. auth/quota/model/clip_set/schema/envelope → 1회
6. 같은 frame map과 같은 exact model을 재사용
7. retry가 durable `attempt_count`를 추가 증가시키지 않음
8. 자동 batch split 없음

- [ ] **Step 5: wrapper 구현**

`sleep`이 필요하면 injectable backoff를 사용하고 test에서는 0으로 둔다. 최대 subattempt=2를 상수로 고정해 env로 무제한 확장하지 않는다.

- [ ] **Step 6: worker process result RED tests 작성**

`process_cli_jobs`가 dict 대신 명시 result type을 반환하도록 변경한다.

Required fields:

- counts by final status
- breaker: `None|auth|quota|model|clip_set|config`
- job ids processed as short identifiers only for logs
- diagnostic counts by phase/code

Assertions:

- auth failure는 remaining camera batch 호출 0
- model mismatch 이후 호출 0
- transient batch terminal failure 후 독립 next camera batch는 계속 가능
- per-clip R2/frame failure는 다른 ready job을 막지 않음
- analyzer batch exception은 ready jobs 모두 같은 safe diagnostic 저장
- succeeded after retry는 `error_code=null`, diagnostic recovered=true
- first-attempt success는 diagnostic null

- [ ] **Step 7: atomic job update 계약 보완**

`status`, `error_code`, `failure_diagnostic`, token/cost/model/result를 한 `update_job` payload로 저장한다. succeeded job을 recovery loader가 다시 선택하지 않는 test를 유지한다.

- [ ] **Step 8: 관련 검증**

```bash
cd /Users/baek/petcam-nightly-reporter
uv run pytest tests/test_claude_cli_analyzer.py tests/test_vlm_worker.py tests/test_vlm_runtime.py -q
```

---

## 6. VLM 전용 Slack summary와 legacy 오해 문구 수정

**Files:**

- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_run_summary.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_run_summary.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/vlm_candidate_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/reporter/worker.py`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_worker.py` 또는 현재 `_format` test 파일

**Interfaces:**

- Add: `VlmRunSummary`
- Add: `format_vlm_run_summary(summary) -> str`
- Add: `send_vlm_run_summary(...)` using existing Slack transport

- [ ] **Step 1: formatter RED tests 작성**

Test exact user-visible cases:

1. 후보 4, 성공 4
2. 후보 0, 호출 0, 정상
3. 일부 R2/frame failure
4. auth breaker
5. model mismatch
6. queued/retryable 30분 초과
7. Slack string에 raw reasoning/path/UUID/email/token 없음
8. KST year/month/day boundary next-run 계산
9. shared lock을 얻지 못한 `blocked_lock` 경고

필수 표시:

```text
🦎 VLM 후보 분석 (07/16 00:00~02:00 KST)
· host: Mac mini · run: 20260716T0200
· 후보 4개: 하이라이트 1 / 미세행동 1 / 다양성 1 / 제외감사 1
· 결과: 성공 4 / 재시도 0 / 실패 0 / 모델보류 0 / 대기 0
· 행동: moving 2 / unseen 2
· 모델: claude-sonnet-5 · Claude 구독 · 직접 API $0
· queue: 정상 (최고 0분) · 다음 04:00
```

후보 0이면 `특이행동 없음` 대신 `후보 0개 · VLM 호출 0회 · 정상 종료`를 쓴다.

- [ ] **Step 2: pure formatter 구현**

DB row의 `result.action`은 allowlisted label formatter를 거친다. 알 수 없는 action은 raw string 전체를 노출하지 말고 `other` count로 묶는다.

- [ ] **Step 3: aggregation RED tests 작성**

candidate run이 생성한 run ids/window/camera와 처리 결과를 다시 조회하거나 in-memory 추적해 다음을 검증한다.

- 이 scheduled invocation에 해당하는 정규 selector만 집계
- recovery와 current를 구분해 표시
- backfill job 제외
- actual model은 성공 row 기준
- model actual 혼합이면 mismatch 경고
- cost는 DB cost 합, subscription이면 직접 API 0으로 표시
- oldest due age는 정규 selector만 계산

- [ ] **Step 4: candidate worker send 경로 구현**

순서:

1. DB terminal updates 완료
2. summary 생성
3. Slack 1회 호출
4. Slack 실패를 catch하고 `slack=FAIL run=<short>` 로그
5. Slack 실패 때문에 process/recovery/analyzer를 재호출하지 않음

`send_fn`을 주입해 tests에서 network 0회로 검증한다.

enabled candidate가 lock을 얻지 못하면 DB/R2/Claude 0회 상태로 `blocked_lock` summary를 1회 보내고 nonzero를 반환한다. scheduled invocation과 수동 duplicate를 process 안에서 확실히 구분할 수 없으므로 둘 다 경고한다. 운영 중 수동 duplicate 실행을 금지하고 run id·host로 경고를 식별한다.

- [ ] **Step 5: legacy 문구 RED test**

`sampled_count=0`일 때 현재 `샘플0: 특이행동 없음`이 아닌 다음을 기대한다.

```text
· 샘플0: VLM 샘플링 꺼짐
```

`sampled_count>0`인 기존 특이행동/실패/unseen 분기는 회귀하지 않는다.

- [ ] **Step 6: legacy `_format` 최소 수정**

`sampled == 0` branch를 signals 판단보다 먼저 명시한다. 이는 VLM 호출을 켜는 변경이 아니라 오해 문구만 고친다.

- [ ] **Step 7: Slack 관련 검증**

```bash
uv run pytest tests/test_vlm_run_summary.py tests/test_vlm_worker.py tests/test_worker.py -q
```

실제 test 파일명이 다르면 `rg -n "샘플0|_format\(" tests`로 찾아 그 파일만 사용한다. 새 중복 test 파일을 만들지 않는다.

---

## 7. launchd installer와 production-equivalent preflight

**Files:**

- Modify: `/Users/baek/petcam-nightly-reporter/install-launchd-vlm-candidate.sh`
- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_install_vlm_launchd.py`
- Create: `/Users/baek/petcam-nightly-reporter/reporter/vlm_preflight.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_preflight.py`

**Interfaces:**

- CLI: `uv run python -m reporter.vlm_preflight`
- Installer required env: `VLM_EXPECTED_HOST=<verified-mac-mini-host>`

- [ ] **Step 1: installer RED tests 작성**

rendered plist assertions:

- 22, 00, 02, 04 KST entries exact
- HOME/USER/LOGNAME/PATH
- `VLM_ROUTER_ENABLED=1`
- `VLM_PROVIDER=claude_cli_batch`
- `ANTHROPIC_MODEL_EXACT=claude-sonnet-5`
- `REGISTER_HIGHLIGHTS=0`
- `VLM_EXPECTED_HOST` exact
- `RunAtLoad` 없음
- `StartInterval` 없음
- stdout/stderr paths

failure assertions:

- expected host missing
- actual host mismatch
- provider direct_api
- enabled != 1 for production install
- Claude executable missing from PATH
- plist lint failure prevents `launchctl bootstrap`

- [ ] **Step 2: installer 최소 수정**

bootstrap 전에 모든 guard를 실행한다. 현재 hostname을 expected 값으로 자동 설정하지 않는다. installation output에는 secret 없이 label/host/schedule/provider/model만 표시한다.

- [ ] **Step 3: preflight RED tests 작성**

preflight는 기본적으로 read-only이며 Claude VLM 호출과 DB write를 하지 않는다.

Checks:

- host exact
- repo branch main
- local HEAD == origin/main (network unavailable는 명시 fail)
- required env present, secret value는 출력하지 않음
- Claude executable resolution
- `claude auth status` loggedIn + subscription method
- exact model config
- system timezone가 Asia/Seoul/KST인지 확인(StartCalendarInterval은 host local timezone을 따름)
- candidate LaunchAgent duplicate host prohibition을 운영 입력으로 검사할 수 있는 JSON output
- R2/checkpoint file prerequisites as applicable
- temp directory writable

Exit:

- all pass → 0
- required fail → nonzero
- 출력은 `PASS/FAIL code=<allowlist>`만, secret 값 없음

- [ ] **Step 4: preflight 구현**

subprocess 호출은 injectable runner를 사용한다. `claude auth status` raw output은 parse 후 버리고 일반 stdout에 재출력하지 않는다.

- [ ] **Step 5: launcher/preflight 검증**

```bash
bash -n install-launchd-vlm-candidate.sh
uv run pytest tests/test_install_vlm_launchd.py tests/test_vlm_preflight.py -q
```

실제 `launchctl bootstrap`은 실행하지 않는다.

---

## 8. Scheduled worker 통합 회귀

**Files:**

- Modify: `/Users/baek/petcam-nightly-reporter/tests/test_vlm_worker.py`
- Modify only if required: worker modules from Tasks 2–7

- [ ] **Step 1: end-to-end fake RED tests 작성**

Supabase fake + fake R2 + fake frames + fake Claude + fake Slack으로 다음 전체 흐름을 검증한다.

### Scenario A: 정상 4개

- host match
- 4 slots selected
- current regular 4개만 process
- all succeeded
- exact model
- cost 0
- Slack 1회
- recovery 0
- forbidden table writes 0

### Scenario B: current 4 + old regular 4 + backfill 4

- current regular 먼저
- old regular은 current 후 bounded recovery
- backfill 0개 소비
- Slack current/recovery 분리

### Scenario C: auth failure

- auth check 1회
- Claude analyze 0회 또는 adapter 계약상 최소 호출
- current jobs terminal/retry contract 준수
- recovery 0
- Slack failure summary 1회

### Scenario D: transient rc1 recovery

- provider call 2회
- durable reservation/attempt 1회
- succeeded + recovered diagnostic
- Slack success 1회

### Scenario E: model mismatch

- held_model_mismatch
- 이후 batch/recovery 중단
- Slack 경고

### Scenario F: Slack webhook failure

- Claude call 수 불변
- succeeded DB row 불변
- run returns documented code
- safe local log only

### Scenario G: duplicate invocation

- lock loser DB/R2/Claude 0회, `blocked_lock` Slack 1회, nonzero
- winner만 처리

### Scenario H: no candidates

- job 0
- Claude 0
- Slack `후보 0개` 1회

- [ ] **Step 2: integration code 최소 보완**

테스트를 위해 production behavior를 바꾸지 않는 dependency injection만 추가한다. module-global monkeypatch 의존을 줄이되 범위 밖 DI framework를 만들지 않는다.

- [ ] **Step 3: 관련 전체 VLM suite**

```bash
uv run pytest \
  tests/test_vlm_host_guard.py \
  tests/test_vlm_runtime.py \
  tests/test_vlm_worker.py \
  tests/test_vlm_backfill_worker.py \
  tests/test_claude_cli_analyzer.py \
  tests/test_vlm_run_summary.py \
  tests/test_vlm_preflight.py \
  tests/test_install_vlm_launchd.py \
  tests/test_install_vlm_backfill.py -q
```

Expected: all green.

---

## 9. 운영 audit와 문서 정합성

**Files:**

- Create: `/Users/baek/petcam-nightly-reporter/reporter/audit_vlm_night.py`
- Create: `/Users/baek/petcam-nightly-reporter/tests/test_audit_vlm_night.py`
- Modify: `/Users/baek/petcam-nightly-reporter/specs/2026-07-15-budgeted-claude-vlm-candidate-router-plan.md`
- Modify: `/Users/baek/petcam-nightly-reporter/specs/2026-07-15-historical-vlm-backfill-plan.md`
- Modify: `/Users/baek/petcam-nightly-reporter/specs/next-session.md`
- Modify: `/Users/baek/petcam-nightly-reporter/.claude/donts-audit.md`

**CLI:** `uv run python -m reporter.audit_vlm_night --date YYYY-MM-DD --json`

- [ ] **Step 1: audit RED tests 작성**

Read-only audit output fields:

- expected windows: 22/00/02/04
- observed regular runs
- missing windows
- producer host distribution
- selected/jobs per window
- status distribution
- stale queued/retryable >30m
- model actual distribution/mismatch
- provider/cost
- regular jobs carrying backfill selector: 0 expectation
- backfill worker regular processing inference via producer host/run ids where possible
- Slack delivery는 DB outbox가 없으므로 `not_verifiable_from_db`로 명시하고 운영 checklist에서 대조

Exit codes:

- all DB-verifiable acceptance pass → 0
- missing window, host mismatch, >4, stale, model mismatch, selector crossover → nonzero

- [ ] **Step 2: read-only audit 구현**

조회만 하고 DB write/R2/Claude/Slack 호출을 하지 않는다. JSON과 human summary 둘 다 제공하되 full UUID를 출력하지 않는다.

- [ ] **Step 3: 기존 문서 정정**

정규 router 문서:

- production host Mac mini 단일화
- global due loader 폐기
- Slack summary 계약
- failure diagnostic/retry matrix

historical backfill 문서:

- regular-first drain 삭제
- backfill selector 전용
- 완료 확인 전 agent 유지

next-session:

- 구현 상태
- migration 미적용
- deployment/canary 미실행
- 현재 MacBook/Mac mini agent 위치는 검증 시각과 함께 historical fact로 기록
- 다음 승인 경계

donts audit:

- queue consumer가 selector ownership을 넘지 않도록 한 줄 추가
- historical backfill을 정규 야간 schedule과 겹치게 설치하지 않도록 한 줄 추가

- [ ] **Step 4: audit tests**

```bash
uv run pytest tests/test_audit_vlm_night.py -q
```

---

## 10. 전체 검증과 코드 리뷰

- [ ] **Step 1: diff overview부터 검토**

```bash
cd /Users/baek/petcam-nightly-reporter
git diff --stat
git status --short
```

기능별 그룹:

1. queue ownership
2. host/installer/preflight
3. CLI diagnostic/retry
4. Slack/legacy wording
5. audit/docs

각 그룹을 순차 검토한다. 한 번에 모든 파일을 덤프하지 않는다.

- [ ] **Step 2: forbidden behavior static audit**

```bash
rg -n "behavior_labels|behavior_logs|REGISTER_HIGHLIGHTS.?=.?1|exclude_static_enabled|exclude_absent_enabled|effective_activity" reporter tests install-launchd-vlm-candidate.sh
rg -n "load_due_jobs\(" reporter
rg -n "regular=.*VLM_SELECTOR_VERSION" reporter/vlm_backfill_worker.py
rg -n "stdout|stderr" reporter/claude_cli_analyzer.py reporter/vlm_candidate_worker.py reporter/vlm_run_summary.py
```

Expected:

- forbidden writes/new activation 0
- candidate global due runtime call 0
- backfill regular drain 0
- raw output persistence/logging 0

- [ ] **Step 3: full nightly tests**

```bash
uv run pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: lab migration static + lab regression**

```bash
cd /Users/baek/petcam-lab
git diff --check
uv run pytest -q
```

Expected: all tests pass. Existing unrelated uncommitted work가 있으면 파일 소유권을 분리해 보고하고 덮어쓰지 않는다.

- [ ] **Step 5: syntax/static checks**

```bash
cd /Users/baek/petcam-nightly-reporter
python -m compileall -q reporter
bash -n install-launchd-vlm-candidate.sh
git diff --check
```

- [ ] **Step 6: 독립 코드 리뷰**

`superpowers:requesting-code-review`를 사용해 다음 관점으로 review한다.

- regular/backfill selector leakage
- host guard ordering
- retry multiplication
- raw secret leakage
- Slack failure causing VLM repeat
- current-window starvation
- succeeded job replay
- deployment race between two hosts

발견 사항은 severity와 파일/라인으로 보고하고 수정 후 관련 test와 전체 test를 다시 실행한다. 수정 loop는 최대 3회다.

- [ ] **Step 7: 구현 종료 보고 후 중단**

반드시 다음을 보고한다.

- 변경 파일 목록
- queue ownership 전/후
- host guard 동작
- retry matrix
- Slack 예시 전문
- test counts
- migration 미적용 확인
- commit/push 미실행 확인
- launchd/DB/Claude 실제 실행 미실행 확인
- 배포 전 남은 위험

여기서 멈춘다. 사용자 승인 없이 Task 11을 실행하지 않는다.

---

## 11. 별도 승인 후 실행할 production 전환 runbook

> 이 task는 구현 plan에 포함되지만 **승인 전 절대 실행하지 않는다.** 한 단계가 실패하면 다음 단계로 넘어가지 않는다.

### Gate A: commit/push

- [ ] 두 repo 작업트리에서 사용자 파일과 구현 파일을 구분한다.
- [ ] 승인된 파일만 explicit add한다.
- [ ] conventional commit을 repo별로 만든다.
- [ ] main 또는 승인 branch에 push한다.
- [ ] local==origin, tree clean/untracked 사용자 파일 보존을 검증한다.

### Gate B: migration apply

- [ ] pre-count를 다시 기록한다.
- [ ] `2026-07-16_clip_vlm_failure_diagnostic.sql`만 적용한다.
- [ ] column/check/comment 존재를 확인한다.
- [ ] job status/count 불변을 확인한다.
- [ ] security advisor에서 새 critical issue 0을 확인한다.

### Gate C: Mac mini preflight

Mac mini에서:

```bash
cd /Users/baek-end/petcam-nightly-reporter
git status --short
git pull --ff-only
VLM_EXPECTED_HOST='<verified-hostname>' uv run python -m reporter.vlm_preflight
```

Checks:

- backup/untracked storage 보존
- main==origin/main
- Claude subscription auth OK
- exact model/config OK
- no secret output

### Gate D: single-host handoff

정규 schedule에서 최소 30분 떨어진 시각에 수행한다.

1. MacBook candidate agent 상태 기록
2. Mac mini candidate agent가 아직 없는지 기록
3. Mac mini system timezone가 Asia/Seoul인지 확인
4. historical backfill process가 현재 running이 아닌지 확인
5. 기존 hourly backfill agent를 bootout하고 daytime calendar installer로 다시 설치
6. backfill plist에 RunAtLoad/StartInterval이 없고 07~19시 entry만 있는지 확인
7. MacBook `com.petcam.vlm-candidate-worker` bootout
8. MacBook agent absent 확인
9. 그 다음에만 Mac mini candidate installer 실행
10. Mac mini candidate plist lint, expected host, provider, model, schedule 확인
11. 두 host 동시 loaded 상태 0초를 목표로 한다.

실패 시 MacBook을 자동 재활성화하지 않는다. 원인 확인 후 사용자에게 rollback 선택을 요청한다.

### Gate E: one-window canary

- [ ] 다음 scheduled window까지 기다린다.
- [ ] run 생성 후 30분 안에 terminal인지 확인한다.
- [ ] producer host=Mac mini only
- [ ] camera·window별 jobs<=4, 전체 night jobs<=64
- [ ] selector crossover=0
- [ ] blocked_lock=0
- [ ] exact model mismatch=0
- [ ] Slack VLM summary 1개
- [ ] Slack message와 DB counts 일치
- [ ] temp mp4/frame 0
- [ ] GT/app/activity writes 0

하나라도 실패하면 agent를 bootout하고, DB job은 삭제/수정하지 않은 채 증거를 보존한다.

### Gate F: historical backfill 유지 판단

- [ ] backfill 진행률과 remaining source dates를 read-only audit한다.
- [ ] queue isolation 적용 후 backfill job만 처리하는지 한 cycle 확인한다.
- [ ] 완료 전이면 유지한다.
- [ ] 완료면 별도 사용자 승인 후 LaunchAgent 제거를 계획한다.

### Gate G: overnight acceptance

다음 날 아침:

```bash
uv run python -m reporter.audit_vlm_night --date YYYY-MM-DD --json
```

Acceptance:

- windows 4/4
- Mac mini host 4/4
- max 4/window
- failures 0 또는 모두 안전 phase 존재
- queued/retryable >30m 0
- model mismatch 0
- selector crossover 0
- blocked_lock 0
- Slack message 4/4 수동 대조
- 직접 API cost 0
- forbidden writes 0

통과 후에만 production 전환 완료로 보고한다.

## 12. 예상 완료 보고 형식

```text
완료 보고 — VLM 단일 호스트 운영 하드닝

1. Queue ownership
- regular: current window → bounded regular recovery
- backfill: backfill selector only
- crossover tests: 0

2. Host ownership
- required expected host: <short host>
- mismatch before DB/Claude/Slack: verified

3. Claude reliability
- max provider subattempts: 2
- breaker: auth/quota/model/clip-set/config
- raw output persistence: 0

4. Slack
- scheduled run summary: implemented
- legacy sample0 wording: VLM 샘플링 꺼짐

5. Verification
- nightly pytest: N passed
- lab pytest: N passed
- static checks: pass

6. Not executed
- migration apply: no
- commit/push: no
- launchd handoff: no
- production Claude call: no

다음 승인: Gate A부터 한 단계씩
```
