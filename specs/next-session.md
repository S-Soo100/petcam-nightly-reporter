# next-session — VLM 단일 호스트 운영 하드닝

> 정본: `docs/superpowers/plans/2026-07-16-vlm-single-host-operations-hardening.md` + `specs/2026-07-16-vlm-single-host-operations-hardening-design.md`
> Slack 운영 개편: `docs/superpowers/plans/2026-07-16-vlm-slack-operations-cleanup.md` · 감사: `reports/vlm-backfill-progress-20260716/REPORT.md`

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
