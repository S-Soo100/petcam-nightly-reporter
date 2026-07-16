# Rolling VLM Backfill 설계 (승인됨, 구현 정본)

> 상태: 사용자 지시문(2026-07-16)으로 설계 승인. 선행 고정형: `specs/2026-07-15-historical-vlm-backfill-plan.md`(superseded).
> CAOF Critical: production DB + Claude 구독 한도 + 정규 VLM 우선권 + Mac mini LaunchAgent 동시 변경.

## 1. 목표
고정 8박(2026-07-07~14, 240개) → **rolling**: 시작일 2026-07-07 고정, 종료일 없음. 완전히 종료된 과거 야간을 매일 자동 발견해, backlog 가 있으면 시간당 ≤30개, 일 ≤600개 처리. backlog 0이면 Claude/Slack 0. "모든 영상 분석"이 아니라 야간별 최대 30개 가치 후보 지속 선정.

## 2. 야간 날짜 계약 (KST)
- source night N = N 20:00 KST ~ N+1 06:00 KST (기존 bucket_plans 유지).
- **완전히 종료된 밤만**: `night_end(N) = N+1 06:00 KST <= now`.
- 순수함수: `latest_closed_source_date(now_kst)`, `rolling_source_nights(start, now_kst)`, `next_rolling_source_date(...)`.
- 금지: 진행 중 오늘 밤 / 미래 / 06:00 전 직전 밤 조기 선택 / UTC↔KST 혼동.
- `bucket_plans` 를 임의 날짜로 일반화: `night_index=(source_date-EPOCH_START).days % 8` (기존 07-07~14 는 동일 index→기존 job window_start 불변, 재생성 0).

## 3. 스케줄 · 정규 VLM 우선권
- 정규 VLM 22/00/02/04, 후보 ≤4, 항상 우선.
- rolling: 매시간 **:35** 실행, cycle ≤30, 일 ≤600.
- **정규 schedule ±30분이면 DB/R2/Gate/Claude 전에 즉시 no-op**(fail-closed guard). 21:35·23:35·01:35·03:35 skip / 22:35·00:35·02:35·04:35 는 lock 비었을 때만.
- shared vlm_lock 유지. 정규가 lock 보유 시 backfill 조용히 양보(lock None → no-op). backfill 때문에 정규가 blocked_lock 되지 않음(정규는 :00, backfill 은 :35 + guard).
- LaunchAgent: 24시간 매시간 :35 (24 entry). 코드 guard 가 정규 근처를 fail-closed 차단. 기존 07~19 calendar 교체.

## 4. 처리량 · 폭주 방지
- cycle ≤30, **KST 일 ≤600 (durable = clip_vlm_jobs backfill 오늘 created_at count)**. 601번째 생성 금지.
- quota/auth/model-mismatch/breaker → 이후 신규 wave 생성 중단(기존 blocking_error + breaker).
- 같은 cycle retry 가 30 상한 우회 못 함(due=한 wave≤30, retry 는 subattempt 내부).
- 직접 API 금지, subscription only, exact model claude-sonnet-5, REGISTER_HIGHLIGHTS=0.

## 5. clip 중복 분석 방지 (cross-selector)
- 후보 선정 전 **모든 clip_vlm_jobs**(regular+backfill, 모든 status) 의 clip_id 집합을 제외.
- queue ownership 은 selector 별 분리 유지(worker 는 backfill selector job 만 처리), **중복 방지는 selector 통합**.
- 동시 두 worker 중복 방지: create RPC `on conflict do nothing`(clip_id, selector_version unique) + ledger 원자 claim.
- 기존 production 120 job 재생성 0(dedup + window_start 불변).

## 6. 부족 후보 날짜 (rolling 핵심)
- 30+ → ≤30. **1~29 → 존재분만 1회 처리** 후 그 밤 재-wave 금지. **0 → no_candidates 로 닫고 다음 날짜**.
- 무한 재스캔 방지 = **ledger** 필수(기존 테이블로 "0개 시도됨"을 표현 불가 → forward migration).
- reopen: closed 밤에 나중 clip 추가 예외는 자동 reopen 안 함(무한루프 방지). 수동 reopen 은 ledger status 를 pending 으로 되돌리는 운영 조치로 한정.

### 6.1 ledger migration (`vlm_backfill_ledger`)
- 컬럼: selector_version, source_date, scope(camera_id), status, target_count, created_count, processed_count, succeeded_count, terminal_count, last_error_code, attempted_at, completed_at. `unique(selector_version, source_date, scope)`.
- status ∈ `pending|processing|completed|no_candidates|insufficient_candidates|blocked`.
- 원자 claim `fn_claim_backfill_source_date`(INSERT ON CONFLICT DO NOTHING→found), 갱신 `fn_upsert_backfill_ledger`. RLS enabled, service_role only, search_path 고정, idempotent.
- pre-ledger 기존 job(07-07~10): 첫 rolling cycle 이 job 상태로 completed/resume 판정 후 ledger 반영(기존 job 재생성 없음).

## 7. 진행률 Slack (rolling)
`📦 과거 영상 VLM 분석 / 실행 장비 / 처리 날짜 / 이번 실행(대상·성공·재시도·영구실패) / 누적 처리(성공·영구실패) / 현재 backlog(진행 중 open · 대기 날짜 수) / 다음 실행 / 정규 VLM 보호: 정상`.
- 고정 "전체 240" 제거(rolling 은 전체 목표 없음). "대기 날짜 N일" = 아직 미처리 closed nights.
- 실제 job 처리 cycle 만 1회. guard skip / lock busy / no backlog / cooldown → 로그만. 신규 날짜 0 → Slack 0. 오류/breaker → 경고 1회.
- 기존 `vlm_slack_notifications` durable dedup 계약 유지(backfill 은 selector+window+host 키; rolling 은 source_date+host 키로 별도). raw reasoning/token/email/UUID/path 금지.

## 8. 실패·복구
- auth/quota/model → 신규 날짜 생성 중단. transient → 기존 subattempt 상한. failed_terminal 자동 재큐 금지.
- submitted crash 고아 job: mark_submitted 후 crash 시 다음 실행이 selector/window 로 resume(bounded). 감사·테스트.
- partial wave 생성 실패 → 다음 실행 멱등 복구(create RPC on conflict). ledger↔jobs 불일치 → 신규 wave 안 만들고 blocked 보고. DB 장애 fail-closed. Slack 장애 무영향. temp mp4/frame 성공·실패 정리(TemporaryDirectory). lock finally 회수.

## 9. 범위 밖
failed_terminal 재큐 · GT/behavior_labels/app activity write · 직접 API · selector 혼합 · 사용자 파일 삭제.
