# Plan — activity-worker Single-Host Migration

설계: `specs/2026-07-16-activity-worker-single-host-design.md`
브랜치: `fix/activity-worker-single-host` (worktree, base `origin/main` = b9dc9eb0…)

## Phase 1 — 코드/테스트 (TDD, RED→GREEN)

### 1.1 config
- `reporter/config.py`: `ACTIVITY_EXPECTED_HOST = os.environ.get("ACTIVITY_EXPECTED_HOST", "")` 추가
  (VLM_EXPECTED_HOST 블록 인접).

### 1.2 activity_worker host guard + partial-failure + 로그 위생
- import: `from reporter.vlm_host_guard import HostOwnershipError, require_expected_host`.
- `run()` 시그니처에 `hostname_fn=socket.gethostname, expected_host=None` 추가.
- 함수 본문 최상단(lock 이전)에 guard:
  ```python
  expected = config.ACTIVITY_EXPECTED_HOST if expected_host is None else expected_host
  try:
      require_expected_host(hostname_fn(), expected)
  except HostOwnershipError as e:
      print(f"[activity] host guard fail-closed: {e}", flush=True)
      return 2
  ```
- 최종 반환: `_log(...)` 뒤 `return 1 if stats["failed"] else 0`.
- `process_batch` clip-skip 로그: `f"[activity] clip {c.id[:8]} skip: {type(e).__name__}"` (예외 전문 제거).

### 1.3 tests/test_activity_worker.py
- 기존 4개 run() 테스트에 `hostname_fn=lambda: "h", expected_host="h"` 주입(guard 통과).
- 신규 RED→GREEN:
  - `test_run_host_match_proceeds`: 일치 host + 빈 카메라 → rc 0, side effect 없음.
  - `test_run_blank_expected_host_fails_closed`: expected "" → rc != 0, lock/create_client/detector 0회.
  - `test_run_host_mismatch_stops_before_side_effects`: host≠expected → rc != 0, 0회.
  - `test_run_partial_failure_returns_nonzero`: 2 clip 중 1 download 실패 → rc == 1, 성공 clip 저장 유지.
  - `test_run_all_ok_returns_zero`: 전부 성공 → rc 0.
  - `test_run_all_fail_returns_nonzero`: 전부 실패 → rc != 0.
  - `test_clip_skip_log_omits_exception_detail`: 예외 메시지에 fake URL/token → stderr 에 미출현, 타입명만.

### 1.4 tests/test_install_activity_launchd.py (신규)
`test_install_vlm_launchd.py` 미러:
- `test_activity_installer_renders_valid_plist`: host/policy/WorkingDirectory/RunAtLoad/StartInterval=3600 검증.
- `test_activity_installer_aborts_when_expected_host_missing`.
- `test_activity_installer_aborts_on_host_mismatch`.
- `test_activity_installer_source_lints_and_no_self_approval_no_secrets`: `plutil -lint` 존재,
  `ACTIVITY_EXPECTED_HOST="$ACTUAL_HOST"` 부재, secret 부재.

### 1.5 install-launchd-activity.sh
- `EXPECTED_HOST`/`ACTUAL_HOST` 계산 + fail-closed 2종 abort.
- plist EnvironmentVariables 에 `ACTIVITY_EXPECTED_HOST` 추가(기존 `ACTIVITY_POLICY_VERSION` 유지).

## Phase 2 — 검증 (§6)
- targeted RED 확인 → GREEN: `uv run pytest tests/test_activity_worker.py tests/test_install_activity_launchd.py -q`
- 전체: `uv run pytest -q`
- `uv run python -m compileall reporter`
- `bash -n install-launchd-activity.sh`
- installer fixture(임시 HOME/stub launchctl) 는 test_install_activity_launchd.py 가 커버.
- `git diff --check`

## Phase 3 — commit + push (§6, fast-forward only)
- conventional commit(feat/fix). `git fetch` 후 origin/main 이 b9dc9eb 이면 fast-forward push,
  변경됐으면 push 중단.

## Phase 4 — handoff gate (§7)
- manifest 작성(handoff_version:1, execution_repo=worktree abs, plan/design abs, commit_sha=최종 HEAD,
  implementation_host=BaekBook…, runtime_kind=launchagent, runtime_host=baeg-endeuui-Macmini.local,
  runtime_label=com.petcam.activity-worker).
- `cd /Users/baek/petcam-lab && uv run python scripts/verify_agent_handoff.py --manifest <abs>` → HANDOFF_OK.

## Phase 5 — 운영 이전 (§8)
- Mac mini preflight(read-only baseline: settings/assessment/prelabel 수/effective view/exclusion snapshot).
- Mac mini pull origin/main → HEAD 40자리 == commit_sha 검증.
- Mac mini render/lint dry check(expected host 명시).
- MacBook: plist timestamp 백업 이동 → `launchctl bootout` → absent 확인(백업 보존).
- Mac mini: bootstrap → lint/host/policy/workingdir 검증 → 두 host 전체 loaded 수 정확히 1.
- 첫 cycle: kickstart → polling → acceptance(hostname/policy/exit/queried=ok/fail=0/증분 정합/evidence identity/중복0/불변).

## Rollback (§9)
- Mac mini 설치/실행 실패 시 MacBook 자동 복구 금지(이중 실행 방지). Mac mini bootout, MacBook plist
  백업 보존, 증거와 함께 중단. DB/exclusion 우회 금지. 데이터 삭제·reset --hard·force push 금지.
