# next-session — VLM 단일 호스트 운영 하드닝

> 정본: `docs/superpowers/plans/2026-07-16-vlm-single-host-operations-hardening.md` + `specs/2026-07-16-vlm-single-host-operations-hardening-design.md`
> Slack 운영 개편: `docs/superpowers/plans/2026-07-16-vlm-slack-operations-cleanup.md` · 감사: `reports/vlm-backfill-progress-20260716/REPORT.md`
> **🔴 2026-07-16 live runtime 재검증 (현재 정본, read-only):** Mac mini `baeg-endeuui-Macmini.local`(KST) production = nightly `main b9dc9eb`. LaunchAgents 전부 exit 0 — `com.petcam.vlm-candidate-worker`(22/00/02/04:00, `VLM_EXPECTED_HOST`=Mac mini, provider `claude_cli_batch`, `VLM_ROUTER_ENABLED=1`, `REGISTER_HIGHLIGHTS=0`), `com.petcam.vlm-historical-backfill`(매시 :35 24×, live 처리 `source=2026-07-12 selected=30 succeeded=19+11`), `com.petcam.vlm-backfill-finalizer`(20:30 one-shot self-unload), `com.petcam.nightly-reporter`(22/00/02/04:05, WINDOW_HOURS=2), `uk.tera-ai.petcam-router-features`(상주 running, WD petcam-lab). **candidate-worker는 Mac mini에 정상 로드**(과거 "MacBook 발견/Mac mini verified 아님" 서술은 **superseded** — single-host hardening 완료). activity-worker는 **MacBook**에만 존재. temp media 0. 통합 하이브리드 설계: `petcam-lab/docs/superpowers/specs/2026-07-16-python-evidence-hybrid-design.md` §0.

## 2026-07-21 (3) — 🔧 launchd auth 장애(auth_probe_failed) 원인 확정·해소

**증상:** 7/20 21:35 UTC부터 candidate/backfill 전 사이클 `auth_probe_failed`(claude exit 1), 재로그인해도 지속. **원인(실측):** claude 자격증명 **이원 저장** — 키체인 항목("Claude Code-credentials")과 `~/.claude/.credentials.json` 파일. **launchd(GUI 도메인)=키체인 우선, SSH=파일 폴백.** 7/20 22:00 KST 키체인 항목 갱신(mdat) 직후 launchd만 `loggedIn:false`. SSH 재로그인은 파일만 갱신 → launchd 미해소. 진단 결정타 = `launchctl submit` 1회성 잡으로 진짜 launchd 스폰 재현(env -i/SSH 재현은 파일을 읽어 못 잡음). **해소(07-21 16:2x):** `security delete-generic-password -s "Claude Code-credentials"` → 전 컨텍스트 파일 폴백 → launchd probe `loggedIn:true` + 적체 15건 처리 재개(submitted 4 실측). ⚠️ 재발 조건: 대화형 재로그인이 키체인 항목 재생성 시 — 같은 삭제로 해소. petcam-lab 메모리 `cron-launchd-keychain` 3차 함정 등재. 밤 카메라 재가동(야간모드) 대비 파이프라인 정상 전제 충족. 잔여: 3e51c7ed는 owner 육안 확정(물 디스펜서 통과=moving 정당, 안정 confabulation 1/42 — petcam-lab decision-gate 기록).

## 2026-07-21 (2) — P1 오탐 재측정 **플랜 B(구독 CLI 약식) 완료: adopt (약식 B)**

배경: A안(temp=0) 실행이 owner 크레딧 결제 콘솔 결함으로 보류 → owner "크레딧 구매 없이" 지시로
약식 프로토콜 B 승인·실행. **A안은 약식 대체가 아니라 결제 해소 후 확정판으로 그대로 유효.**

- **pre-reg**: `experiments/label-determinism-remeasure/TEST-SHEET-B.md` (`f1f541e`, 사후 변경 없음).
  표본 = A안 sample_list 42건 그대로. 로컬 `claude -p` 구독(2.1.177), exact `claude-sonnet-5`,
  v4.0 append, 6장@768q85, `--json-schema` 7-class, 클립당 3회. 판정: **3/3 원 오탐 라벨 재현만
  "진짜 오탐(강)"**, 게이트 ≤25% adopt (A안 동일).
- **결과** (`REPORT-B.md` + `results_b.json`): **126/126콜 완주, 진짜 오탐(강) 0/42 (shedding 0/32 ·
  drinking 0/10) → `adopt` (약식 B)**. 3회 일치율 83.3%(35/42) — 비-unanimous 7클립 = temp>0 비결정성
  직접 증거. 126런 라벨분포 moving 70/unseen 48/drinking 4/shedding 4. 토큰 ≈6.17M(대부분 cache),
  $0(구독), 한도/인증 실패 0. 관찰: unseen 38% = 6장@768 입력 정보부족 신호, `3e51c7ed` drinking 3/3
  안정 오분류(신규 GT 주목), 스크립트 `scripts/remeasure_label_determinism_b.py`(+27 tests).
- **실행 사고(결과 무영향)**: 13:39~43 외부 요인으로 1차 배치 진행분 삭제+프로세스 종료(untracked
  results만 소실, 커밋 파일 무손상) → 재실행 완주. REPORT-B §6-4.
- **다음**: P1 결정론 배선(Task 4, owner 승인 게이트) 방향 유지 · decision-gate 회신(Task 6) ·
  P2는 강 잔존 0이라 보류(A안 확정 후 재판단).

## 2026-07-21 — P1 오탐 재측정 (Task 3+5): pre-reg 완료, **A안 실행은 ANTHROPIC_API_KEY 대기로 차단**

plan `docs/superpowers/plans/2026-07-21-nightly-label-determinism-plan.md` Task 3+5. owner "자동진행" 위임분.

- **Task 3 완료 (pre-reg 동결):** `experiments/label-determinism-remeasure/TEST-SHEET.md` + `sample_list.json`(42건 = shedding FP 32 + drinking FP 후보 10, r2_key·duration DB SELECT 실측 고정). 게이트: 진짜 오탐 잔존율 ≤25% adopt / 25~50% hold / >50% reject, 3회 일치율 기대 ≥95%, 예상 $1.0~2.1, 하드 게이트 $5.
- **배치 스크립트 준비 완료 (TDD GREEN):** `scripts/remeasure_label_determinism.py` + `tests/test_remeasure_label_determinism.py`(24 tests, 전체 306 passed). v4.0 핀은 파일 무수정 런타임 override(`pin_v40()` — production main `618f4f8` 동치 프롬프트+7-class 스키마), exact `claude-sonnet-5`, temp=0, 6장@768q85, 클립당 3회, durable results.json+resume, 일시 에러만 backoff≤3, 4xx·model-mismatch 즉시 중단, production 테이블 쓰기 0.
- **⛔ Task 5 실행 차단:** `.env`에 `ANTHROPIC_API_KEY` 부재 (`.env.example`엔 direct_api용으로 문서화돼 있으나 실 `.env` 미설정, 셸 env에도 없음). 계약상 키 생성/요청 금지 → preflight가 exit 2로 fail-closed 확인. **owner가 키를 `.env`에 넣으면 `uv run python scripts/remeasure_label_determinism.py` 1회 실행 → results.json 요약 출력 → REPORT.md 작성 + decision-gate 회신(Task 6)** 순으로 재개.

## activity-worker 단일 호스트 이전 (2026-07-16, VERIFIED)

- **runtime = Mac mini** (`baeg-endeuui-Macmini.local`), service `com.petcam.activity-worker` loaded. MacBook(`BaekBook-Pro-14-M5.local`) 는 absent(plist 비파괴 백업 `~/Library/LaunchAgents/activity-worker-decommissioned-20260716-162235/`). 두 호스트 전체 loaded 수 = 1.
- **final HEAD** = `3610f15c753a248679c5b1897e140320b1b97a5a` (origin/main, fast-forward). fix: host guard(`ACTIVITY_EXPECTED_HOST` fail-closed) + partial-failure nonzero exit + 로그 위생.
- **첫 실사이클(VERIFIED)**: `cameras=3 queried=88 ok=88 fail=0 active=61 absent=2 static=5 unknown=20` exit 0, policy=activity-v1, model=gecko_v2. DB assessments/prelabels +88(전량 Mac mini producer), evidence 7컬럼 결손 0·중복 0. exclusion settings·behavior_labels(258)·clip_vlm_jobs(229) 불변.
- 정본: `specs/2026-07-16-activity-worker-single-host-design.md` · `docs/superpowers/plans/2026-07-16-activity-worker-single-host-migration.md`.

## 배포 상태 (2026-07-16 04:xx KST) — deployed, 실사이클 검증 대기

- **커밋/push 완료**: nightly `6bbdd55`(Slack 4종 + preflight fix), lab `26ba0ed`(router 억제). main==origin/main.
- **Mac mini 단일 candidate 전환 완료(deployed)**: MacBook `com.petcam.vlm-candidate-worker` bootout(absent 확인) → Mac mini 설치(enabled=1 · provider=claude_cli_batch · model=claude-sonnet-5 · VLM_EXPECTED_HOST=`baeg-endeuui-Macmini.local` · plist lint OK · 22/00/02/04). preflight 9/9 PASS. 두 호스트 동시 loaded 0.
  - ⚠️ 배포 시점 MacBook candidate 는 이미 host guard 로 exit 3(no-op)였음 — 신규 job 생성 없었음(fail-closed 이미 작동).
- **backfill 07~19 calendar 재설치(deployed)**: RunAtLoad/StartInterval 0, calendar-hours 13.
- **movement 2h 재설치(deployed)**: SAMPLE_TOP_N=0(legacy Claude 차단) · WINDOW_HOURS=2 · 22:05/00:05/02:05/04:05.
- **router metadata 억제(verified)**: lab pull 후 worker 재시작 → 프로덕션 로그에 `Slack suppressed (no-op cycle)` 실측 확인.
- **production 데이터 무변경**: 배포 전후 clip_vlm_jobs total 109 / latest 07-15 17:00 UTC 동일.

### 실사이클 검증 대기 (verified 아님)
- 정규 candidate: 다음 **22:00 KST** window — producer host=Mac mini, exact model, Slack `🦎 VLM 행동 분석` 1회, DB count 대조.
- backfill: 다음 **07:00 KST** cycle — Slack `📦 과거 영상 VLM 분석`, 누적 DB 대조.
- movement: 다음 **22:05 KST** — `📊 움직임 수집 요약`, Claude 0.

## 구현 상태 (2026-07-16) — 선행 단일호스트 하드닝

Task 1~10 을 TDD 로 구현·검증 완료 후 위와 같이 배포됨.

- **Queue ownership** — 정규 worker: current window/selector → bounded 정규 recovery(≤4). backfill worker: `BACKFILL_SELECTOR_VERSION` 전용 + 07~19시 KST daytime guard. 전역 `load_due_jobs()` 런타임 사용 0.
- **Host ownership** — `reporter/vlm_host_guard.py` fail-closed. enabled worker 는 `VLM_EXPECTED_HOST` 불일치 시 DB/Claude/Slack 전 nonzero 종료.
- **Claude reliability** — redacted `CliFailureDiagnostic`, durable attempt 당 subretry ≤2(retryable 만), auth/quota/model/clip-set breaker. 원문 stdout/stderr 미저장.
- **Slack** — `reporter/vlm_run_summary.py` VLM 전용 요약 1회. legacy `_format` sampled==0 → `VLM 샘플링 꺼짐`.
- **installer/preflight** — candidate installer 는 host/provider/model/enabled/claude-PATH/plist-lint guard. `reporter/vlm_preflight.py` read-only preflight. backfill installer 는 07~19시 calendar.
- **audit** — `reporter/audit_vlm_night.py` read-only night 수용 audit(`--date --json`).

## 미실행 (별도 사용자 승인 필요 — Task 11 Gate A~G)

- **migration 미적용** — `petcam-lab/migrations/2026-07-16_clip_vlm_failure_diagnostic.sql` 작성만 됨(forward-only, idempotent). apply 안 함.
- **commit/push 미실행.**
- **deployment/canary 미실행** — MacBook LaunchAgent bootout / Mac mini bootstrap / 실제 Claude canary 전부 미실행.

## 현재 호스트 상태 (historical fact, 검증 시각 명기 필요)

- 검증 당시 정규 candidate LaunchAgent 는 **MacBook 에 설치**돼 있었고 Mac mini 에는 없었다(설계 §2.3). MacBook 초기 batch 는 terminal/retryable 실패로 끝났고, Mac mini backfill worker 가 정규 due job 을 교차 처리한 것으로 추론된다(producer host/job timestamps 로 재검증 대상).
- 이 상태는 배포 전 실측으로 재확인하고 Gate D handoff 로 정정한다.

## 다음 승인 경계

Task 11 Gate A(commit/push) → B(migration apply) → C(Mac mini preflight) → D(single-host handoff) → E(one-window canary) → F(backfill 유지 판단) → G(overnight acceptance) 순으로 **한 단계씩** 사용자 승인 후 진행.

## 2026-07-16 (2) — Slack 후속 하드닝 4건 (deployed, dedup 실사이클 대기)

- **backfill 진행률 의미 정정**(deployed): 처리(succeeded+terminal)/성공/영구실패/진행중/미생성/남은처리 분리. 남은 처리=240−처리(영구실패 미포함). ETA=남은 처리 기준. formatter=live DB 직접 대조 통과(처리 112/240·남은 128·미생성 120).
- **정규 VLM Slack durable dedup**(deployed): (selector+window_start+window_end+host) 원자 claim. migration `2026-07-16_vlm_slack_notifications`(fn_claim/fn_release) **production 적용 완료**(advisor 신규 critical 0, INFO rls_no_policy=service_role infra 의도). Slack 실패 시 claim 해제→재전송. 다음 22:00 candidate cycle 로 실검증.
- **API 비용 정직성**(deployed): cost>0 이면 실제 USD+경고, 0일 때만 '0원'. provider≠claude_cli_batch 면 구독 단정 안 함.
- **MacBook candidate plist 백업·제거**: `~/petcam-launchd-backups/20260716T092615/` 로 비파괴 이동, LaunchAgents 에서 absent. 두 호스트 candidate loaded 정확히 1(Mac mini).
- 커밋: nightly `0c85052`, lab `ba1aaf3`. Mac mini pull 완료(0c85052/ba1aaf3), 모듈 compile OK.

## 2026-07-16 (3) — Rolling backfill 전환 (코드·migration deployed, Mac mini 반영 PENDING)

정본: `specs/2026-07-16-rolling-vlm-backfill-design.md` + `docs/superpowers/plans/2026-07-16-rolling-vlm-backfill.md`.
고정 8박(2026-07-15-historical-vlm-backfill-plan.md) → **rolling superseded**(history 보존).

- **커밋/push 완료**: nightly `81f3b57`, lab `e95187f`(ledger migration). main==origin.
- **ledger migration production 적용 완료·검증**: `vlm_backfill_ledger` + `fn_claim_backfill_source_date`(원자 claim first=true/dup=false 실측)/`fn_upsert_backfill_ledger`. advisor 신규 critical 0(INFO rls_no_policy=service_role infra). 합성 row 정리 후 ledger 0행.
- **production 데이터 무변경**: total 139(backfill 120·regular 19) pre==post, ledger 0행.
- ⚠️ **Mac mini rolling 반영 PENDING**: 배포 시점 Mac mini(home-mac/100.78.155.5) SSH **연결 불가**(3회 timeout, Tailscale 미도달 — 호스트 sleep/오프라인 추정). Mac mini 는 여전히 **구 고정 backfill 코드(e5a0823) + 07~19 :00 plist** 로 정상 동작 중(07-10 진행). ledger·rolling 코드 미사용(dormant). 부분 배포/손상 없음.

### Mac mini 반영 재개 절차(호스트 도달 시, 정규 VLM ±30분 밖)
1. `ssh home-mac` 도달 확인 → `cd ~/petcam-nightly-reporter && git pull --ff-only`(→81f3b57), `cd ~/petcam-lab && git pull --ff-only`(→e95187f).
2. compile 확인 → 기존 backfill 미실행 확인 → `launchctl bootout gui/$(id -u)/com.petcam.vlm-historical-backfill`.
3. `bash install-launchd-vlm-backfill.sh`(24× :35) → plist 24 entry·:35·guard 확인.
4. 첫 허용 cycle(:35, 정규 ±30분 밖) 관찰 → Slack `📦 과거 영상 VLM 분석`(대기 날짜 포함)·DB·로그·temp 0 대조.
- rollback: 재설치 실패 시 구 07~19 :00 installer 자동 복구 금지, 증거 보존·보고.

## 2026-07-16 (4) — Rolling backfill 배포차단 H1~H5 보완 + Mac mini 반영·첫 cycle 검증 완료

- 커밋: nightly `45992ed`(H1~H5), lab `df7811c`(fn_release_backfill_claim). main==origin.
- migration: `2026-07-16_vlm_backfill_ledger` + `2026-07-16_vlm_backfill_claim_release` **production 적용·검증**(claim first/dup, release jobs→blocked/no-jobs→released, advisor 신규 critical 0).
- **Mac mini rolling 배포 완료**: 구 backfill bootout → rolling installer(VLM_EXPECTED_HOST=`baeg-endeuui-Macmini.local`) → plist lint OK · 24× :35 · legacy trigger 0 · host/provider/model/REGISTER=0 명시.
- **첫 실사이클(kickstart 10:39~10:49) VERIFIED**: `source=2026-07-11 selected=30`(첫 미생성 밤 자동 발견) · 성공 19 · 재시도 11 · non-exact model 0 · ledger processing · **regular 19 불변(crossover 0)** · dup clip 0 · created-today 30(<600) · Slack 전송 성공(slack=FAIL 없음) · temp mp4/frame 0 · exit 0.
- H1 claim(07-11 processing) · H2 host guard(plist env) · H3 pagination · H4 deadline(process 전달) · H5 정합성 전부 런타임 반영.
- 다음 :35 cycle 이 07-11 잔여 11 retryable resume → 완료 후 07-12 new 진행(rolling 자동).

## 2026-07-16 (5) — VLM 휴식(basking) 분류 복구 (feature branch, canary 전 / production 미반영)

정본: `specs/2026-07-16-vlm-basking-classification-design.md` + `docs/superpowers/plans/2026-07-16-vlm-basking-classification.md`. 브랜치 `feat/vlm-basking-classification`.

- **v4.1 계약 구현·local tests GREEN**: 새 `resting` enum 없이 기존 저장 enum `basking` 복구. prompt `system.v4.1.md` / `VLM_PROMPT_VERSION=v4.1-direct-images`, CLI·직접API schema 8-class parity(직접 API 비활성 유지). basking=자동 하이라이트 제외, Slack `휴식` 표시(기타 미붕괴). full pytest 282 passed.
- **11건 blind 분포**: unseen 3 / moving 3 / basking 5(그중 제품 제외 2건 `ab273d21`,`ab8cd4b0` — 제품 제외는 행동 라벨과 분리, 실험 보고서에만). manifest `experiments/vlm-basking-20260716/human-blind-manifest.json`(short ID·기대 행동만, UUID/영상/reasoning 미커밋).
- **Claude canary는 Mac mini 전용**: `scripts/evaluate_vlm_basking_canary.py`(host guard→shared lock→media 1:1→auth→4/4/3, DB/Slack 미import). MacBook에서 Claude 미호출. **아직 production main 미반영** — Mac mini 격리 worktree canary 수용 기준 통과 시에만 fast-forward.
- **DB migration 없음, 기존 150건 재분류 없음, GT/behavior_labels/app activity/Gate 불변.** 새 코드 배포 후 새로 성공하는 job부터 basking 가능.
- 커밋: Task1 `51faef3` · Task2 `bf51bb5` · Task3 `68e6315`.

## 2026-07-16 (6) — 휴식 canary 결과: REJECTED (main/production 미반영)

Mac mini 격리 worktree canary(`db37c279`, host guard+shared lock 통과, infra_failed 0, 4/4/3) 결과 **accepted=false → 수용 기준 미달**. 설계 §7.2/§9에 따라 **main merge·Mac mini production pull 중단**. production main `b9dc9eb` 불변, 두 LaunchAgent 불변, DB 무변경(v4.1 job 0·basking 0·regular 19·canary clip 여전히 v4.0).

- 수용 기준: unseen=3·moving=3·basking≥4·basking→unseen=0. 실측: **unseen 3✓ / moving 1✗ / basking 2✗ / basking→unseen 2✗**.
- v4.1 개선점: basking 0→2 회복(ad1772c6·1d34eb48), a3774a4f unseen 회복.
- v4.1 부작용: ca27e1f3 moving→basking 퇴행(moving 3→1), 941aadb9·ab8cd4b0 basking→unseen(critical error 1→2).
- 원인 가설: v4.1 basking 경계가 "몸 위치 고정" 쪽으로 과회전 → 실제 이동(ca27e1f3)을 basking으로, 가림 큰 휴식(941aadb9/ab8cd4b0)을 여전히 unseen으로. 프롬프트 재조정 필요(basking↔unseen 가림 처리 + basking↔moving 미세 이동 경계). 결과 로컬 보존: `petcam-lab/storage/retry-review-20260711/vlm-basking-v4.1-canary.json`, baseline `pre-v4.1-retry-baseline.json`.
- feature branch `feat/vlm-basking-classification`는 유지(코드·계약·canary 하네스는 GREEN). 다음: 프롬프트 재조정 후 동일 canary 재실행.
