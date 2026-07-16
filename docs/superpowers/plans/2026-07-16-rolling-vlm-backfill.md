# Rolling VLM Backfill Implementation Plan

> 정본 설계: `specs/2026-07-16-rolling-vlm-backfill-design.md`. Critical track. TDD RED→GREEN.
> 선행 고정형 `specs/2026-07-15-historical-vlm-backfill-plan.md` 은 superseded(삭제 금지, history).

**Goal:** 고정 8박 backfill → rolling(시작 2026-07-07, 종료 없음, closed night 자동 발견, 시간당 ≤30/일 ≤600, 정규 VLM 우선, cross-selector clip dedup, 부족 날짜 durable 처리).

## 구현 순서 (모듈별 TDD)

1. **rolling 날짜 순수함수** (`reporter/vlm_backfill_selector.py`): `EPOCH_START`, `latest_closed_source_date`, `rolling_source_nights`, `bucket_plans` 일반화(`night_index=(d-EPOCH).days%8`). 테스트: 종료된 밤만, 00~05시 진행중 제외, 월말/연말 KST, 신규 자동 추가.
2. **schedule guard** (`reporter/vlm_rolling.py`): `rolling_backfill_allowed_now(now)` — 정규 22/00/02/04 ±30분 no-op, 그 외 허용. 테스트: 21:35/23:35/01:35/03:35 skip, 22:35~04:35 허용, guard 가 DB/Claude 전.
3. **daily cap** (`reporter/vlm_rolling.py` + store): `backfill_created_today(sb, now)` durable(clip_vlm_jobs backfill created_at KST 오늘), `remaining_daily_budget`. 테스트: ≤600, 601 금지, retry 우회 불가.
4. **cross-selector clip dedup** (`reporter/vlm_backfill_worker.py` prepare_wave): 후보에서 모든 clip_vlm_jobs clip_id 제외. 테스트: regular succeeded/queued 제외, backfill 기존 제외, wave 내 unique, 동시 claim, 기존 120 재생성 0.
5. **부족 후보 처리** (prepare_wave): `!=30` raise 제거 → 0→no_candidates, 1~29→존재분 처리, ≤30 clamp. WavePlan.selected_count.
6. **ledger** (lab migration `2026-07-16_vlm_backfill_ledger.sql` + `reporter/vlm_store.py` + FakeSB): 테이블 + `fn_claim_backfill_source_date`/`fn_upsert_backfill_ledger`. RLS/service_role/search_path/idempotent.
7. **worker rolling loop** (`run()`): schedule guard → lock → daily cap → `next_rolling_source_date`(closed nights ∩ ledger ∩ jobs) → resume(open) or new wave(dedup, ≤30, ledger claim) → process → ledger update → Slack.
8. **rolling Slack** (`reporter/vlm_backfill_summary.py`): rolling 필드(전체 240 제거, 대기 날짜 수, backlog). dedup 키 source_date+host.
9. **installer** (`install-launchd-vlm-backfill.sh` + test): 24× :35 calendar, 07~19 교체.

## 검증
nightly/lab 전체 pytest, compileall, bash -n, plutil 렌더, git diff --check, migration rollback probe, advisor 신규 critical 0, production pre/post-count(기존 120·regular 불변), crossover 0, dup clip 0, temp 0.

## 배포 (Critical, 정규 ±30분 밖)
backfill process 종료 확인 → pre-count → migration apply·검증 → commit/push → Mac mini pull → preflight → 기존 backfill bootout → rolling installer bootstrap → 24× :35 확인 → guard 확인 → 첫 허용 cycle 관찰 → Slack/DB/로그 대조 → temp 0. 실패 시 자동 복구 금지, rollback 보고.
