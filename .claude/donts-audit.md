# donts-audit — petcam-nightly-reporter

> Standard 이상 작업 후 한 줄 추가. Three-Strike Rule — 같은 실수 3회 시 정식 룰 승격.

## 2026-07-16 — VLM 단일 호스트 운영 하드닝

- queue consumer 는 **자기 selector ownership 을 넘지 않는다** — 정규 worker 는 정규 selector/window 만, backfill worker 는 `BACKFILL_SELECTOR_VERSION` 만 처리한다. 전역 `load_due_jobs()` 로 남의 queue 를 drain 하지 않는다(§7). 이번 하드닝의 근본 결함 = 정규/backfill queue 교차 소비.
- historical backfill 은 **정규 야간 schedule(22/00/02/04 KST)·shared Claude lock 과 겹치게 설치하지 않는다** — 07~19시 정각 calendar 로만 실행한다. 상시/주기 트리거(`RunAtLoad`/`StartInterval`)를 쓰면 야간 lock 경합으로 정규 후보가 굶는다(§7.2).

## 2026-07-16 (2) — Slack 운영 메시지 개편

- Slack 은 **정규 VLM 행동분석 / historical backfill 진행률 / 움직임 수집 / router metadata 4종을 명확히 분리**한다. no-op cycle(조회0·완료0·실패0)은 로그만 남기고 Slack 을 억제한다 — "30분마다 0건 반복"이 운영자 신뢰를 깎았다.
- 움직임 수집 요약은 반드시 "VLM 분석 결과가 아님"을 명시하고, **SAMPLE_TOP_N=0** 으로 legacy Claude 호출을 차단해 candidate worker 와의 중복 Claude 호출을 막는다.
- 진행률/누적 메시지는 **selector 전용 집계**로만(정규↔backfill 혼입 금지), 실제 처리 cycle 에서만 1회, 완료는 최종 date cycle 에서만 1회(반복 금지).

## 2026-07-16 (3) — Slack 후속 하드닝

- **진행률은 worker 의 완료 계약과 일치**시킨다. backfill 은 `succeeded+failed_terminal`을 '처리'로, `240−처리`를 '남은 처리'로 표시한다. failed_terminal 은 worker 가 재처리 안 하므로 '남은'에 넣으면 모순(완료율이 영원히 안 참). ETA 도 성공이 아니라 남은 처리량 기준.
- **scheduled 알림은 durable idempotency**로 보낸다. 실행시각 run_id + 프로세스 메모리로는 수동 재실행·동시 실행 중복을 못 막는다. (selector+window+host) unique + INSERT ON CONFLICT 원자 claim, 전송 실패 시 claim 해제로 재전송.
- **비용은 정직하게**. 계산값을 무시하고 '0원' 하드코딩 금지 — >0 이면 실제값+경고, provider 로 구독 여부 단정 금지.
