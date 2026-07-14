# Claude Code CLI Batch Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Code 구독으로 카메라별 후보 최대 4개를 호출 1회에 분석하고 기존 DB shadow job 원장에 안전하게 기록한다.

**Architecture:** `claude_cli_analyzer.py`가 CLI 호출과 envelope 검증만 담당한다. worker는 provider에 따라 direct API의 clip별 처리 또는 CLI의 run/camera batch 처리를 선택한다. selector와 기존 activity worker는 변경하지 않는다.

**Tech Stack:** Python 3.12, pytest, Claude Code CLI, Supabase, launchd.

## Global Constraints

- exact model은 `claude-sonnet-5`다.
- batch는 카메라·window당 최대 4 clip, clip당 6 JPEG다.
- shadow-only이며 behavior_logs/camera_clips를 쓰지 않는다.
- 실제 API 청구액은 `$0`, 환산 비용은 result JSON에만 보존한다.
- 기존 direct API provider와 activity worker를 보존한다.

---

### Task 1: CLI batch analyzer

**Files:**
- Create: `reporter/claude_cli_analyzer.py`
- Create: `tests/test_claude_cli_analyzer.py`

**Interfaces:**
- Consumes: `dict[clip_id, list[Path]]`, exact model ID.
- Produces: `analyze_batch(frame_sets, model, runner=subprocess.run) -> CliBatchResult`.

- [x] JSON schema, command 제한, 성공 envelope, 인증 오류, 모델 불일치, clip ID 집합 불일치 테스트를 먼저 작성한다.
- [x] `uv run pytest tests/test_claude_cli_analyzer.py -q`가 import/기능 부재로 실패하는지 확인한다.
- [x] 최소 analyzer 구현 후 같은 테스트를 통과시킨다.

### Task 2: Worker provider 분기

**Files:**
- Modify: `reporter/config.py`
- Modify: `reporter/vlm_candidate_worker.py`
- Modify: `tests/test_vlm_worker.py`

**Interfaces:**
- Consumes: due jobs, `VLM_PROVIDER=claude_cli_batch`.
- Produces: 카메라·selector_run별 최대 4 job batch와 clip별 DB result.

- [x] batch grouping, R2 실패 격리, batch breaker, subscription provenance 테스트를 먼저 작성한다.
- [x] 대상 테스트의 RED를 확인한다.
- [x] provider 분기와 batch 처리 최소 구현 후 대상 테스트·전체 pytest를 통과시킨다.

### Task 3: LaunchAgent와 운영 문서

**Files:**
- Modify: `install-launchd-vlm-candidate.sh`
- Modify: `.env.example`
- Modify: `tests/test_install_vlm_launchd.py`

- [x] plist에 `VLM_PROVIDER=claude_cli_batch`, exact model, shadow 설정이 있는지 실패 테스트를 작성한다.
- [x] RED 확인 후 launcher를 수정하고 테스트·`bash -n`·`plutil -lint`를 통과시킨다.

### Task 4: Production canary와 활성화

- [x] `claude-sonnet-5` 최소 인증 probe에서 `modelUsage` exact key를 확인한다.
- [x] 실제 00~02시 후보 4개 canary를 실행해 DB succeeded row, usage, `pricing_version`, 앱 write 0을 확인한다.
- [x] `VLM_ROUTER_ENABLED=1`로 LaunchAgent를 설치한다.
- [x] launchctl plist/일정/환경과 기존 activity worker 생존을 확인한다.
- [x] SOT에 실행 시각, 첫 결과, 실패 조건, 롤백 명령을 기록하고 commit/push한다.

## 운영 상태 — 2026-07-15 02:30 KST

- `com.petcam.vlm-candidate-worker`가 22·00·02·04시 KST에 실행되며 provider는
  `claude_cli_batch`, 모델은 exact `claude-sonnet-5`다.
- 첫 canary는 00~02시 44개 clip 중 4개만 선택했고 4/4 성공했다. 요청 모델과 실제 모델이
  모두 Sonnet 5였고 `cost_usd=0`, `reserved_cost_usd=0`,
  `pricing_version=claude-code-subscription-v1`을 확인했다.
- 결과는 `clip_vlm_selector_runs`/`clip_vlm_jobs`에만 저장한다. 앱 하이라이트,
  `behavior_logs`, `camera_clips`, 활동시간에는 반영하지 않는다.
- launchd 강제 smoke는 이미 처리한 동일 창을 재호출하지 않고 `stats={}`, exit 0으로 끝났다.
  기존 `com.petcam.activity-worker`도 runs 7, last exit 0으로 유지된다.
- 즉시 중단: `launchctl bootout gui/$(id -u)/com.petcam.vlm-candidate-worker`.
  DB job은 감사·재현 원장이므로 중단 시에도 삭제하지 않는다.
