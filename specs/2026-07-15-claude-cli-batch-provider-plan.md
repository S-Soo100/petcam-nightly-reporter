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

- [ ] JSON schema, command 제한, 성공 envelope, 인증 오류, 모델 불일치, clip ID 집합 불일치 테스트를 먼저 작성한다.
- [ ] `uv run pytest tests/test_claude_cli_analyzer.py -q`가 import/기능 부재로 실패하는지 확인한다.
- [ ] 최소 analyzer 구현 후 같은 테스트를 통과시킨다.

### Task 2: Worker provider 분기

**Files:**
- Modify: `reporter/config.py`
- Modify: `reporter/vlm_candidate_worker.py`
- Modify: `tests/test_vlm_worker.py`

**Interfaces:**
- Consumes: due jobs, `VLM_PROVIDER=claude_cli_batch`.
- Produces: 카메라·selector_run별 최대 4 job batch와 clip별 DB result.

- [ ] batch grouping, R2 실패 격리, batch breaker, subscription provenance 테스트를 먼저 작성한다.
- [ ] 대상 테스트의 RED를 확인한다.
- [ ] provider 분기와 batch 처리 최소 구현 후 대상 테스트·전체 pytest를 통과시킨다.

### Task 3: LaunchAgent와 운영 문서

**Files:**
- Modify: `install-launchd-vlm-candidate.sh`
- Modify: `.env.example`
- Modify: `tests/test_install_vlm_launchd.py`

- [ ] plist에 `VLM_PROVIDER=claude_cli_batch`, exact model, shadow 설정이 있는지 실패 테스트를 작성한다.
- [ ] RED 확인 후 launcher를 수정하고 테스트·`bash -n`·`plutil -lint`를 통과시킨다.

### Task 4: Production canary와 활성화

- [ ] `claude-sonnet-5` 최소 인증 probe에서 `modelUsage` exact key를 확인한다.
- [ ] 1 clip canary를 실행해 DB succeeded row, usage, `pricing_version`, 앱 write 0을 확인한다.
- [ ] `VLM_ROUTER_ENABLED=1`로 LaunchAgent를 설치한다.
- [ ] launchctl plist/일정/환경과 기존 activity worker 생존을 확인한다.
- [ ] SOT에 실행 시각, 첫 결과, 실패 조건, 롤백 명령을 기록하고 commit/push한다.

