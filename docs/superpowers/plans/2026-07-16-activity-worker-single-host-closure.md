# Activity Worker Single-Host Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 움직임 Slack 시간창의 소수 시간 정밀도 버그를 복구하고, 최종 Git/runtime 정합성과 `activity-worker`의 자연 두 번째 실행을 증명해 single-host 이전을 완전히 마감한다.

**Architecture:** `reporter.worker._format()`의 표시 시간 계산만 최소 수정하고 30분·2시간 계약을 단위 테스트로 고정한다. 코드 검증과 fast-forward 배포 후 handoff manifest를 최종 commit에 맞추고, Mac mini의 기존 LaunchAgent를 재설치하거나 강제 실행하지 않은 채 다음 `StartInterval` 자연 실행을 관측한다. 실행 결과는 기존 migration report를 수정하지 않고 별도 closure report로 남긴다.

**Tech Stack:** Python 3.12, pytest, launchd LaunchAgent, Git, Supabase read-only audit, macOS SSH

## Global Constraints

- 실행 레포는 `/Users/baek/petcam-nr-activity-wt`이며 현재 clean worktree의 `fix/activity-worker-single-host` 브랜치를 사용한다.
- 기존 `/Users/baek/petcam-nightly-reporter`의 `feat/vlm-basking-classification` checkout과 미추적 reliability 문서는 수정·삭제·커밋하지 않는다.
- Mac mini runtime repo는 `/Users/baek-end/petcam-nightly-reporter`, hostname은 `baeg-endeuui-Macmini.local`이다.
- `com.petcam.activity-worker` plist, schedule, policy, expected host는 변경하거나 재설치하지 않는다.
- 자연 두 번째 실행은 `kickstart`, 수동 Python 실행, plist 재bootstrap으로 대체하지 않는다.
- DB setting, exclusion switch, GT, `behavior_labels`, `clip_vlm_jobs`, VLM selector/batch, Gate threshold는 변경하지 않는다.
- 기존 `/Users/baek/petcam-lab/docs/handoff-prompts/2026-07-16-activity-worker-single-host-migration-report.md`는 수정하지 않는다.
- 최종 보고서는 `/Users/baek/petcam-lab/docs/handoff-prompts/2026-07-16-activity-worker-single-host-migration-closure-report.md`에 작성한다.
- raw URL, token, API key, UUID 원문, 전체 예외 메시지를 보고서와 로그에 기록하지 않는다.

---

### Task 1: Slack 시간창 소수 정밀도 복구

**Files:**
- Modify: `tests/test_worker.py`
- Modify: `reporter/worker.py:99-101`

**Interfaces:**
- Consumes: `reporter.config.WINDOW_HOURS: float`, `_NOW = 2026-07-16 02:05 KST`
- Produces: `_format()`이 `WINDOW_HOURS=0.5`에서 `01:30~02:00`, `WINDOW_HOURS=2.0`에서 `00:00~02:00`을 표시한다.

- [ ] **Step 1: 기존 실패를 재현한다**

Run:

```bash
cd /Users/baek/petcam-nr-activity-wt
uv run pytest -q tests/test_worker.py::test_format_movement_summary_shape
```

Expected: FAIL. 현재 기본값 `WINDOW_HOURS=0.5`가 `int(0.5)=0`으로 잘려 `02:00~02:00`이 나온다.

- [ ] **Step 2: 2시간 계약을 테스트 안에서 명시한다**

`tests/test_worker.py`의 import와 기존 shape test를 다음처럼 바꿔:

```python
from reporter import config
from reporter.worker import _format


def test_format_movement_summary_shape(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_HOURS", 2.0)
    msg = _format(_activity(), _beh(), _NOW)
    assert "📊 움직임 수집 요약 (00:00~02:00)" in msg
    assert "· 감지 클립 27개 · 약 14.5분" in msg
    assert "· 집중 시간대: 1시" in msg
    assert "이 메시지는 움직임 수집 통계이며 VLM 분석 결과가 아님" in msg
```

- [ ] **Step 3: 30분 회귀 테스트를 추가하고 RED를 확인한다**

`tests/test_worker.py`에 추가:

```python
def test_format_preserves_fractional_window_hours(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_HOURS", 0.5)
    msg = _format(_activity(), _beh(), _NOW)
    assert "📊 움직임 수집 요약 (01:30~02:00)" in msg
```

Run:

```bash
uv run pytest -q tests/test_worker.py
```

Expected: 새 30분 테스트만 FAIL하며 실제 문자열은 `02:00~02:00`이다.

- [ ] **Step 4: 최소 구현으로 정밀도 손실을 제거한다**

`reporter/worker.py`에서 다음 한 줄만 변경해:

```python
disp_start = disp_end - timedelta(hours=config.WINDOW_HOURS)
```

`int()` 제거 외의 formatter 문구·Slack 구조·조회 시간창 로직은 변경하지 않는다.

- [ ] **Step 5: targeted tests를 GREEN으로 만든다**

Run:

```bash
uv run pytest -q tests/test_worker.py
```

Expected: `5 passed`.

- [ ] **Step 6: 변경 범위를 확인한다**

Run:

```bash
git diff -- reporter/worker.py tests/test_worker.py
git diff --check
```

Expected: `int()` 제거, config import, 테스트 1개 추가 외의 변경이 없고 `git diff --check` exit 0.

---

### Task 2: 전체 검증과 최종 코드 commit

**Files:**
- Include in commit: `reporter/worker.py`
- Include in commit: `tests/test_worker.py`
- Include in commit: `docs/superpowers/plans/2026-07-16-activity-worker-single-host-closure.md`

**Interfaces:**
- Consumes: Task 1의 GREEN 상태와 시작 당시 `origin/main` SHA
- Produces: 전체 테스트가 통과한 final commit SHA 1개

- [ ] **Step 1: 전체 검증을 실행한다**

Run:

```bash
uv run pytest -q
uv run python -m compileall reporter
bash -n install-launchd-activity.sh
git diff --check
```

Expected: pytest 실패 0, compileall/bash/diff-check exit 0. 하나라도 실패하면 commit/push/runtime 작업을 중단하고 closure report에 `BLOCKED`로 기록한다.

- [ ] **Step 2: commit 대상만 명시적으로 stage한다**

Run:

```bash
git status --short
git add reporter/worker.py tests/test_worker.py docs/superpowers/plans/2026-07-16-activity-worker-single-host-closure.md
git diff --cached --check
git diff --cached --stat
```

Expected: 세 파일만 staged. 다른 세션 파일이나 사용자 파일은 포함되지 않는다.

- [ ] **Step 3: commit한다**

Run:

```bash
git commit -m "fix: 움직임 요약 시간창 소수 정밀도 복구"
```

Expected: conventional commit 성공. `FINAL_SHA=$(git rev-parse HEAD)`를 40자리로 기록한다.

- [ ] **Step 4: fast-forward 안전성을 확인하고 push한다**

Run:

```bash
git fetch origin
git merge-base --is-ancestor origin/main HEAD
test "$(git rev-parse HEAD^)" = "$(git rev-parse origin/main)"
git push origin HEAD:main
test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | cut -f1)"
```

Expected: 시작 이후 예상하지 못한 remote commit이 없고 force 없이 main fast-forward. 조건이 다르면 push하지 않고 `BLOCKED`로 보고한다.

---

### Task 3: Final SHA handoff와 Mac mini 코드 정합

**Files:**
- Modify outside execution repo: `/Users/baek/petcam-lab/docs/handoff-prompts/2026-07-16-activity-worker-single-host-manifest.md`
- Read only: `/Users/baek/petcam-lab/scripts/verify_agent_handoff.py`

**Interfaces:**
- Consumes: Task 2의 `FINAL_SHA`
- Produces: 현재 시점에도 재현 가능한 `HANDOFF_OK`, Mac mini `HEAD == origin/main == FINAL_SHA`

- [ ] **Step 1: manifest를 final commit으로 정합한다**

먼저 final SHA를 검증해:

```bash
FINAL_SHA="$(git -C /Users/baek/petcam-nr-activity-wt rev-parse HEAD)"
test "${#FINAL_SHA}" -eq 40
printf '%s\n' "$FINAL_SHA"
```

그다음 manifest를 `apply_patch`로 수정해. `commit_sha`에는 위 명령이 출력한 실제 40자리 값을 그대로 넣고, 나머지 값은 다음 계약을 사용해:

```yaml
handoff_version: 1
task_id: activity-worker-single-host-closure
execution_repo: /Users/baek/petcam-nr-activity-wt
plan_path: /Users/baek/petcam-nr-activity-wt/docs/superpowers/plans/2026-07-16-activity-worker-single-host-closure.md
design_path: /Users/baek/petcam-nr-activity-wt/specs/2026-07-16-activity-worker-single-host-design.md
implementation_host: BaekBook-Pro-14-M5.local
runtime_kind: launchagent
runtime_host: baeg-endeuui-Macmini.local
runtime_label: com.petcam.activity-worker
```

- [ ] **Step 2: handoff validator를 실행한다**

Run:

```bash
cd /Users/baek/petcam-lab
uv run python scripts/verify_agent_handoff.py \
  --manifest /Users/baek/petcam-lab/docs/handoff-prompts/2026-07-16-activity-worker-single-host-manifest.md
```

Expected: `HANDOFF_OK task=activity-worker-single-host-closure`이며 출력의 `commit` 값은 `git rev-parse --short=8 HEAD`의 출력과 같고, runtime은 `launchagent@baeg-endeuui-Macmini.local`이다.

- [ ] **Step 3: Mac mini가 idle인지 확인하고 final commit을 pull한다**

Run:

```bash
ssh home-mac 'launchctl print gui/$(id -u)/com.petcam.activity-worker | grep -E "state =|runs =|last exit code"'
ssh home-mac 'cd ~/petcam-nightly-reporter && git pull --ff-only origin main && git rev-parse HEAD && git status --short --branch'
```

Expected: pull 전 worker가 `not running`; pull 후 HEAD가 `FINAL_SHA`. 기존 `.env.bak-*`는 보존하고 commit/delete하지 않는다.

- [ ] **Step 4: plist를 변경하지 않았음을 확인한다**

Run:

```bash
ssh home-mac 'plutil -p ~/Library/LaunchAgents/com.petcam.activity-worker.plist | grep -E "ACTIVITY_EXPECTED_HOST|ACTIVITY_POLICY_VERSION|StartInterval|WorkingDirectory"'
```

Expected: expected host `baeg-endeuui-Macmini.local`, policy `activity-v1`, `StartInterval=3600`, 기존 working directory 유지. bootout/bootstrap/kickstart를 실행하지 않는다.

---

### Task 4: 자연 두 번째 cycle 관측

**Files:**
- Read only: MacBook/Mac mini launchctl, `/tmp/activity-worker.log`, production Supabase

**Interfaces:**
- Consumes: Task 3의 loaded LaunchAgent와 첫 RunAtLoad `runs=1`
- Produces: launchd `StartInterval`이 만든 `runs>=2` 증거와 Mac mini-only DB 증분

- [ ] **Step 1: 자연 실행 시점까지 기다린다**

첫 RunAtLoad는 2026-07-16 16:23 KST에 끝났으므로 다음 자연 실행은 대략 17:23 KST 이후야. 현재 시간이 이미 지났으면 즉시 검사해. 아직이면 blocking sleep 대신 20~30초 간격의 짧은 polling을 사용하되, `kickstart`, 수동 모듈 실행, plist 재bootstrap은 금지한다.

- [ ] **Step 2: 두 번째 launchd run을 확인한다**

Run:

```bash
ssh home-mac 'launchctl print gui/$(id -u)/com.petcam.activity-worker | grep -E "state =|runs =|last exit code"; grep "^\[activity\].*queried=" /tmp/activity-worker.log | tail -3'
```

Expected: `runs>=2`, 가장 최근 `last exit code=0`. 새로운 summary가 있으면 `fail=0`; 처리 대상 0인 정상 early-return이라 summary가 없다면 해당 시간의 no-work 로그와 DB 미처리 0 근거를 함께 기록한다.

- [ ] **Step 3: MacBook 재기동이 없음을 확인한다**

Run:

```bash
launchctl print gui/$(id -u)/com.petcam.activity-worker 2>&1
test ! -e "$HOME/Library/LaunchAgents/com.petcam.activity-worker.plist"
```

Expected: service not found, plist absent. 백업 plist는 보존돼 있다.

- [ ] **Step 4: production DB를 read-only 대조한다**

Supabase MCP `execute_sql`로 다음만 집계해:

- `clip_activity_assessments`의 producer_host별 count/max(created_at)
- 최신 producer_run_id별 count
- Mac mini `clip_prelabels` 7컬럼 identity 결손 수
- 7컬럼 identity 중복 그룹 수
- `camera_activity_filter_settings`의 세 스위치와 policy version
- `behavior_labels`, `clip_vlm_jobs` 총계

Expected:

- 이전 이후 MacBook 신규 assessment 0
- 자연 두 번째 run의 신규 assessment가 있다면 전량 Mac mini producer
- identity 결손 0, 중복 0
- settings unchanged
- `behavior_labels`, `clip_vlm_jobs`는 이 작업 때문에 증가하거나 변경되지 않음

- [ ] **Step 5: temp/log hygiene를 확인한다**

Run:

```bash
ssh home-mac 'find /tmp -maxdepth 2 -type f \( -name "*.mp4" -o -name "*frame*" \) -print; grep -Eci "cloudflarestorage|token=|SUPABASE_SERVICE|r2\." /tmp/activity-worker.log || true'
```

Expected: temp media 출력 0, secret pattern count 0.

---

### Task 5: Closure report 작성

**Files:**
- Create: `/Users/baek/petcam-lab/docs/handoff-prompts/2026-07-16-activity-worker-single-host-migration-closure-report.md`
- Preserve: `/Users/baek/petcam-lab/docs/handoff-prompts/2026-07-16-activity-worker-single-host-migration-report.md`

**Interfaces:**
- Consumes: Tasks 1~4의 fresh command output
- Produces: Codex가 독립 검수할 단일 closure report

- [ ] **Step 1: 보고서를 다음 순서로 작성한다**

```markdown
# Activity Worker Single-Host Migration — Closure Report

## 1. 최종 판정
VERIFIED 또는 BLOCKED

## 2. 시간창 버그 원인과 수정
## 3. RED→GREEN 및 전체 테스트 결과
## 4. final commit SHA와 origin/main
## 5. HANDOFF_OK 전문과 manifest 경로
## 6. Mac mini HEAD/plist/launchctl
## 7. 자연 두 번째 cycle 증거
## 8. MacBook absent 재확인
## 9. DB producer/identity/settings/금지 테이블 대조
## 10. temp media와 secret scan
## 11. 미검증 또는 잔여 위험
## 12. Codex가 SOT에 반영할 정확한 문구
```

수치와 SHA는 실제 fresh output만 사용하고, 실행하지 않은 검증을 통과로 적지 않는다.

- [ ] **Step 2: 문서 경계와 작업트리를 확인한다**

Run:

```bash
git -C /Users/baek/petcam-nr-activity-wt status --short --branch
git -C /Users/baek/petcam-lab status --short -- \
  docs/handoff-prompts/2026-07-16-activity-worker-single-host-migration-report.md \
  docs/handoff-prompts/2026-07-16-activity-worker-single-host-migration-closure-report.md \
  docs/handoff-prompts/2026-07-16-activity-worker-single-host-manifest.md
```

Expected: execution repo는 final commit 기준 clean. 기존 migration report는 내용 불변이며 closure report와 manifest만 별도 산출물로 표시된다.

- [ ] **Step 3: 채팅에는 문서 경로만 보고한다**

Claude 채팅 응답은 다음 두 줄 이내로 제한해:

```text
최종 판정: VERIFIED|BLOCKED
보고서: /Users/baek/petcam-lab/docs/handoff-prompts/2026-07-16-activity-worker-single-host-migration-closure-report.md
```

Codex가 보고서, 양쪽 host, Git, DB를 다시 독립 검수하기 전에는 SOT 최종 마감으로 주장하지 않는다.
