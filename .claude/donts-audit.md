# donts-audit — petcam-nightly-reporter

> Standard 이상 작업 후 한 줄 추가. Three-Strike Rule — 같은 실수 3회 시 정식 룰 승격.

## 2026-07-16 — VLM 단일 호스트 운영 하드닝

- queue consumer 는 **자기 selector ownership 을 넘지 않는다** — 정규 worker 는 정규 selector/window 만, backfill worker 는 `BACKFILL_SELECTOR_VERSION` 만 처리한다. 전역 `load_due_jobs()` 로 남의 queue 를 drain 하지 않는다(§7). 이번 하드닝의 근본 결함 = 정규/backfill queue 교차 소비.
- historical backfill 은 **정규 야간 schedule(22/00/02/04 KST)·shared Claude lock 과 겹치게 설치하지 않는다** — 07~19시 정각 calendar 로만 실행한다. 상시/주기 트리거(`RunAtLoad`/`StartInterval`)를 쓰면 야간 lock 경합으로 정규 후보가 굶는다(§7.2).
