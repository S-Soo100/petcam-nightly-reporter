# VLM Slack 운영 메시지 개편 Implementation Plan

> 정본 지시: 사용자 2026-07-16 요청. 선행: `docs/superpowers/plans/2026-07-16-vlm-single-host-operations-hardening.md`.
> 감사: `reports/vlm-backfill-progress-20260716/REPORT.md`.

**Goal:** 정규 VLM 행동분석 / historical backfill 진행률 / 움직임 수집 / 라우터 metadata 4종 Slack 을 명확히 분리하고, no-op·중복·오해 메시지를 제거한다. legacy Claude 중복 호출을 차단한다.

**Tech Stack:** Python 3.12, pytest(TDD), Supabase, launchd, Slack webhook, uv. 새 DB migration 없음.

## 스코프 (4종 + 스케줄)

### A. 정규 VLM 행동 분석 — `reporter/vlm_run_summary.py` (nightly-reporter)
- 제목 `🦎 VLM 행동 분석 (HH:MM~HH:MM)`, `· 실행 장비: {host} · run {HHMM}`.
- `· 후보: N개 / 실제 분석: M개`(M=후보−대기), 선정 슬롯 한글 라벨(`·` 구분).
- `· 결과: 성공·재시도·실패·모델보류`(대기>0 시 추가), 행동 **한글 라벨**(moving→일반이동, drinking→핥기, unseen→게코 안 보임 …), `· 모델: Claude Sonnet 5 구독 · 직접 API 비용 0원`, `· 큐: 정상/⚠️지연 · 다음 분석 HH:MM`.
- 계약: 후보0 → `후보 0개 · Claude 호출 0회 · 정상 종료`. blocked_lock/host/auth/quota/model 경고. run_id 중복 방지(이미 process 내 1회). raw reasoning/UUID/token/path 미노출.

### B. historical backfill 진행률 — 신규 `reporter/vlm_backfill_summary.py` + `vlm_backfill_worker` 연동
- 제목 `📦 과거 영상 VLM 분석`, 실행 장비/처리 날짜/이번 실행(대상·성공·재시도·실패)/누적(완료 X / 전체 240)/남은 영상/예상 완료(coarse)/다음 실행.
- 집계는 `BACKFILL_SELECTOR_VERSION` **전용**(정규 selector 혼입 금지).
- **실제 backfill job 을 처리한 cycle 에서만** 1회 전송. outside-hours/cooldown/no-op/incomplete-wave-skip 은 로그만.
- 최종 source date 완료 cycle 에서만 완료 메시지 1회(이미 완료 상태 반복 금지). retryable/terminal/mismatch 분리.

### C. 움직임 수집 요약 — `reporter/worker.py` + `install-launchd.sh`
- 제목 `📊 움직임 수집 요약 (HH:MM~HH:MM)`, 감지 클립·활동분·집중 시간대 + `이 메시지는 움직임 수집 통계이며 VLM 분석 결과가 아님`.
- `WINDOW_HOURS=2`, `SAMPLE_TOP_N=0`(legacy Claude 차단), 클립0 → Slack 0(로그만, 기존 동작 유지).
- installer: RunAtLoad/StartInterval 제거 → 22:05/00:05/02:05/04:05 KST calendar.

### D. 라우터 metadata — `backend/router_features.py` (petcam-lab)
- `queried=0 && completed=0 && failed=0` → Slack 미전송(로그만). 실제 처리 시 concise 1개. failed>0/stale → 즉시 경고.
- `R2+OpenCV metadata only · LLM/VLM 호출 없음`은 실제 처리 메시지에만. metadata 생성/DB/provenance 동작 불변.

## 방식
CAOF Standard. 각 formatter/suppression/누적계산/중복방지를 **순수함수**로 TDD(RED→최소구현→GREEN). 범위 밖 리팩터 금지. 새 migration 없음(필요 시 보고 후 중단). 비밀값/raw Claude 출력 영속화·로그 금지.

## 검증
양쪽 전체 pytest, compileall, `bash -n` 변경 installer, plutil 임시 HOME 렌더, `git diff --check`, selector crossover 정적·동적, Slack formatter exact-string. baseline: nightly 229 / lab 381.

## 배포 (정본 Gate D 순서, 안전 시각)
정규 schedule ±30분 밖에서: MacBook candidate bootout → absent 확인 → Mac mini candidate 설치(expected host/provider/model/lint) → backfill daytime calendar → movement 2h calendar → router metadata 반영. 두 호스트 candidate 동시 loaded 금지. 실제 Slack 테스트 전송 금지 — 다음 실제 cycle 로 검증.
