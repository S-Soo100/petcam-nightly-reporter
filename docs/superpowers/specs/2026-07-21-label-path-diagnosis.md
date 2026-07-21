# Nightly 행동 라벨 경로 진단 (Task 1) + 오탐 표본 (Task 2)

> plan: [`../plans/2026-07-21-nightly-label-determinism-plan.md`](../plans/2026-07-21-nightly-label-determinism-plan.md) Task 1·2 산출물. read-only 진단 — 코드/운영 무변경.
> 실측 시각: **2026-07-21 12:0x~12:13 KST**, Mac mini `baeg-endeuui-Macmini.local` SSH(`home-mac`) + Supabase SELECT + 로컬 git.

## 1. 결론 요약

| 오탐 | 생산 경로 | 호출 방식 | temperature 제어 |
|---|---|---|---|
| **탈피(shedding) 오탐** (owner가 본 야간 IR→shedding, ~2026-07-07/08) | `reporter/worker.py` → `reporter/classify.py` (`com.petcam.nightly-reporter`) | `claude -p --model sonnet` (CLI) | **불가** |
| **쳇바퀴→drinking 오탐** (후보 2계열, §5 표본) | ① `behavior_logs` 잔존분 = 같은 `classify.py` 계열(nightly-board auto / motion-backfill B_sat, `claude-sonnet-4-6`) ② `clip_vlm_jobs` = `vlm_backfill_worker`/`vlm_candidate_worker` (`claude_cli_batch`) | 둘 다 `claude -p` (CLI) | **둘 다 불가** |
| (참고) 결정론 경로 | `reporter/anthropic_analyzer.py` (Messages API) | SDK `client.messages.create` | **temperature=0 명시** — 단 **production 미배선** |

**핵심:** owner-visible 라벨을 만들었던/만들고 있는 경로는 전부 Claude CLI(`claude -p`) 기반이고, **Claude CLI에는 temperature 제어 옵션이 존재하지 않는다** (§3). temperature=0이 실제로 걸리는 코드는 `anthropic_analyzer.py`뿐인데, production은 `VLM_PROVIDER=claude_cli_batch`라 이 경로를 타지 않는다.

**⚠️ 기록 정정 필요:** petcam-lab `specs/next-session.md` 2026-07-19 블록과 `experiments/vlm-care-label-audit-20260719/REPORT.md` §0의 "daily `com.petcam.vlm-candidate-worker`(Sonnet v4.0, **temp=0**)" 서술은 **오류**다. 실측 근거: Mac mini `launchctl print` env `VLM_PROVIDER=claude_cli_batch` + DB `clip_vlm_jobs.result.provider="claude_cli_batch"` (해당 감사 11건 포함 기간) + CLI에 temperature 플래그 부재. 즉 **07-19 감사의 dish-confabulation 10/11은 temperature 비결정 경로에서 나온 것**이며, "temp=0인데도 오탐"이라는 해석은 성립하지 않는다.

## 2. 경로 인벤토리 (코드 + 런타임 실측)

### 2-A. `com.petcam.nightly-reporter` — `worker.py` → `classify.py` (구 shedding 오탐 경로)

- 호출: `claude -p … --append-system-prompt-file prompts/system.v4.0.md --model sonnet --output-format json` (`reporter/classify.py:34-41`, production main `618f4f8` 기준 v4.0. 현 feature branch에선 v4.1로 바뀌어 있음 — §4).
- temperature 인자 없음(CLI가 지원 안 함). 모델도 exact ID가 아닌 alias `sonnet`.
- owner 노출: Slack `📊 움직임 수집 요약` + informative 라벨의 `camera_clips`/`behavior_logs` 자동 등록(`register.py`).
- **현재 상태 (실측):** 2026-07-16 배포부터 plist env `SAMPLE_TOP_N=0` → **Claude 호출 0, 라벨 생산 0**. `launchctl print gui/$(id -u)/com.petcam.nightly-reporter` env `WINDOW_HOURS=2, SAMPLE_TOP_N=0`, `/tmp/nightly-reporter.log` 07-18~07-21 전 실행 `sampled=0 … actions={}` 확인.
- shedding 오탐 귀속 근거: `reporter/config.py` `REGISTER_SKIP_ACTIONS` 주석(2026-07-08, 흰 모프 개체 밤 IR 상시 오탐, 누적 22건 전부 실제 moving) + petcam-lab `experiments/v41-shedding-ir-guard/REPORT.md`(같은 오탐 32건을 adaptive@1080 **결정론 조건** 재추론 시 v4.0·v4.1 모두 64/64 moving = 오탐 재현 0 → 원인은 temperature 비결정성).

### 2-B. `com.petcam.vlm-candidate-worker` + `com.petcam.vlm-historical-backfill` — `claude_cli_batch` (현재 유일한 라벨 생산 경로)

- 호출: `reporter/claude_cli_analyzer.py analyze_batch()` — `claude -p … --safe-mode --model claude-sonnet-5 --effort low --system-prompt-file prompts/system.v4.0.md --json-schema …` (production main 기준 v4.0).
- temperature 인자 없음. `--effort low`만 지정.
- owner 노출: Slack `🦎 VLM 행동 분석`의 **행동 분포 라인**(`vlm_run_summary.py` — drinking은 `핥기`로 표기) + `clip_vlm_jobs.result`. `REGISTER_HIGHLIGHTS=0`(plist 실측)이라 앱/라벨링 큐로는 안 나감.
- **런타임 실측:** Mac mini `launchctl print` — candidate: `VLM_ROUTER_ENABLED=1, VLM_PROVIDER=claude_cli_batch, VLM_EXPECTED_HOST=baeg-endeuui-Macmini.local, ANTHROPIC_MODEL_EXACT=claude-sonnet-5, REGISTER_HIGHLIGHTS=0`, last exit 0. backfill: 동일 provider env. Mac mini repo HEAD = `618f4f8` (= origin/main tip), branch main.
- drinking 오탐 귀속: `clip_vlm_jobs`에서 07-05 이후 succeeded 452건 중 drinking은 정확히 2건, 둘 다 `provider=claude_cli_batch`(backfill selector `budget-router-backfill-20260707-14-v1`, completed 07-16) — 07-19 감사에서 이미 **GT=moving 오탐 확정**된 그 2건.

### 2-C. `reporter/anthropic_analyzer.py` — Messages API (결정론, 미배선)

- `client.messages.create(model=…, temperature=0, output_config=json_schema, cache_control)` (`anthropic_analyzer.py:17`). exact model 강제, 비용 추적 연동.
- 배선: `vlm_candidate_worker.run()`에서 `VLM_PROVIDER != "claude_cli_batch"`일 때만 `process_jobs()`가 사용. production은 `claude_cli_batch`이므로 **호출 0회**. 어떤 LaunchAgent도 이 경로로 라벨을 만들고 있지 않다.

## 3. Claude CLI temperature 제어 가능 여부 — **불가 확정**

- MacBook `claude` 2.1.177, Mac mini `/opt/homebrew/bin/claude` 2.1.204 (cask, symlink 2026-07-16).
- `claude --help | grep -iE "temperature|sampling|top-p|top_p"` → **매치 0** (양쪽 버전 공통). 존재하는 인퍼런스 관련 플래그는 `--model`, `--effort`, `--fallback-model`, `--betas`뿐.
- 따라서 `classify.py`·`claude_cli_analyzer.py` 어느 쪽도 서브프로세스 인자로 temperature를 걸 수 없다. CLI를 유지한 채 결정론을 얻는 방법은 없음 → 결정론 배선(Task 4)은 **`anthropic_analyzer.py`(Messages API) 경로 전환**이 유일한 코드 레버 (design §2와 일치).

## 4. 프롬프트 버전 상태 (design §5 함정 실측)

| 위치 | classify.py | claude_cli_analyzer.py | anthropic_analyzer.py | config.VLM_PROMPT_VERSION |
|---|---|---|---|---|
| **production main `618f4f8`** (Mac mini 실행 중) | `system.v4.0.md` | `system.v4.0.md` | `system.v4.0.md` | `v4.0-direct-images` |
| **feature branch `feat/vlm-basking-classification` `46ca39e`** (이 작업 브랜치) | `system.v4.1.md` | `system.v4.1.md` | `system.v4.1.md` | `v4.1-direct-images` |

- v4.1(basking 복구)은 2026-07-16 canary **REJECTED** (`specs/next-session.md` (6)) — main 미반영이 맞는 상태.
- **Task 4 주의:** 이 브랜치에서 결정론 배선을 하면 v4.1이 암묵 승계된다. design §5대로 **v4.0 핀을 명시 결정**해야 함 (TEST-SHEET 항목).
- classify.py docstring은 "v4.0 프롬프트 주입"이라 쓰여 있는데 브랜치 코드는 v4.1 파일을 가리킴 — docstring drift (코드 수정 금지 범위라 기록만).

## 5. 오탐 표본 (Task 2)

### 5-A. shedding 오탐 세트 — **32건 복원 완료**

- 출처: petcam-lab `experiments/v41-shedding-ir-guard/sample_list_fp.json` (읽기 전용).
- 32건 전부 `clip_id + r2_key + gt(전부 moving) + gt_store(human/labels)` 보유. 카메라 `p4cam-27b1f486`/`p4cam-79b5d844`, 녹화 2026-07-03~07-07.
- 재현 기록: 같은 실험 REPORT에서 결정론 조건(adaptive@1080) 재추론 시 v4.0/v4.1 모두 64/64 moving — 이번 재측정의 baseline 기대치.

### 5-B. 쳇바퀴→drinking 오탐 후보 — **10건 목록화 (owner 확인 필요)**

DB SELECT 실측 (2026-07-21, 쓰기 0회):

**계열 ① `clip_vlm_jobs` (claude_cli_batch, 07-19 감사에서 GT=moving 확정):**

| clip_id | completed | conf | 비고 |
|---|---|---|---|
| `6c16a62b-f572-4008-84dc-b6c700777ada` | 07-16 21:37 | 0.55 | 감사 GT=moving("밥그릇 근처 이동") — **쳇바퀴 여부 owner 확인 필요** |
| `b0171f2d-329d-43b7-8cbb-b742065ad1b6` | 07-16 21:37 | 0.55 | 상동 |

**계열 ② `behavior_logs` source=vlm, `claude-sonnet-4-6` (classify.py 계열, 앱/라벨링 큐 노출 가능 경로) — 8건 전수:**

| clip_id | 녹화(UTC) | camera | notes | conf |
|---|---|---|---|---|
| `439e1798-a7fc-4368-8dda-835fb5a6b51a` | 07-05 18:24 | p4cam-27b1f486 | motion-backfill B_sat v4.0 | 0.65 |
| `3db14864-cc42-4a8e-b077-ba249d3cfdc4` | 07-06 18:57 | p4cam-79b5d844 | motion-backfill B_sat v4.0 | 0.68 |
| `ad4bd25e-0d38-4464-9923-b22bf952a870` | 07-06 20:40 | p4cam-79b5d844 | motion-backfill B_sat v4.0 | 0.60 |
| `135c6248-7205-4ccc-b2cf-3bf62e7cc8b1` | 07-06 20:49 | p4cam-79b5d844 | motion-backfill B_sat v4.0 | 0.62 |
| `25ee99b0-dec6-472a-b0bb-b78fa4c5eafd` | 07-07 13:34 | p4cam-79b5d844 | nightly-board auto | 0.68 |
| `e679f8ad-9011-4bc2-a489-1bb93c54ead8` | 07-07 13:40 | p4cam-79b5d844 | nightly-board auto | 0.65 |
| `a57ce7cd-77cb-473a-b935-191111186acd` | 07-07 19:02 | p4cam-79b5d844 | nightly-board auto | 0.62 |
| `29a74166-1024-4bdd-a497-b1133a86549b` | 07-07 20:11 | p4cam-79b5d844 | nightly-board auto | 0.62 |

- **owner 확인 필요:** 이 10건 중 어느 것이 "쳇바퀴 반복 동작" 클립인지 + 목록 밖(Slack에서만 보고 DB에 없는) 클립이 따로 있는지. 전부 verified=false, corrected_to=null 상태.
- (참고) 그 외 behavior_logs drinking은 2026-05-02 gemini v3.5 import 7건뿐 — 이번 스코프 밖(historical).
- 표본 sample_list 파일 고정은 owner 확인 후 Task 3(TEST-SHEET)에서 수행.

## 6. ⚠️ 부수 발견 — 2026-07-20 20:00 KST부터 VLM 워커 전량 auth_probe_failed (진행 중 장애)

Task 1 실측 중 발견. **이번 작업 범위 밖이라 조치 안 함** — owner 판단 필요.

- `/tmp/vlm-candidate-worker.log`: 07-20 20:00 KST(11:00Z) 창부터 4개 창 연속 `auth_probe_failed`/`breaker='auth'` (직전 07-19 19:00Z 창까지는 정상 succeeded). `/tmp/vlm-historical-backfill.log`: 07-21 11:35 KST까지 `source=2026-07-20 due=15` 매시 전량 auth 실패 반복.
- 피해: 07-20 밤 정규 VLM 라벨 0건, recovery에서 07-18 세트 4 job `failed_terminal` 처리됨, backfill 07-20분 15 job 적체.
- **모순 실측:** 12:10 KST 현재 Mac mini에서 `claude auth status` = `loggedIn: true` (SSH 셸 + launchd plist PATH 재현 `env -i` 시뮬레이션 둘 다 성공, 0.25s, rc 0). binary `/opt/homebrew/bin/claude` 2.1.204 (07-16 설치, 07-19까지 정상 동작). DB `failure_diagnostic`: `exit_code: 1, stdout_bytes: 0, stderr_bytes: 0, markers: []` — **`claude auth status`가 launchd 컨텍스트에서 출력 0바이트 + rc 1로 즉사**하는 패턴. redaction 설계상 원문이 없어 사후 원인 특정 불가.
- 다음 자연 사이클(12:35 backfill / 22:00 candidate)이 회복 여부를 알려줌. 회복 안 되면 launchd 컨텍스트 전용 요인(keychain ACL 등) 조사 필요 — LaunchAgent 조작은 승인 필요라 이 세션에서 미실행.

## 7. Task 4(결정론 배선) 방향 시사 — 착수 금지, 기록만

1. 유일 레버 = candidate/backfill worker의 provider를 `direct_api`(`anthropic_analyzer.py`, temperature=0)로 전환. plist env `VLM_PROVIDER` 변경 = **launchd 변경 + Messages API 유료 활성화 → 둘 다 사용자 승인 STOP** (design §7).
2. 프롬프트 v4.0 핀 명시 결정 필요 (§4 — 이 브랜치는 v4.1이 기본값이라 암묵 승계 위험).
3. `classify.py` 경로는 현재 라벨 생산 0(SAMPLE_TOP_N=0)이라 배선 우선순위 아님 — 재활성화하려면 같은 이유로 Messages API 전환이 선행돼야 함.
