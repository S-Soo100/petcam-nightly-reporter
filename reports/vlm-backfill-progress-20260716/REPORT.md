# Historical VLM Backfill 진행률 감사 (read-only)

- 감사 시각: 2026-07-16 03:47 KST (UTC 2026-07-15 18:47)
- 감사자: BaekBook-Pro-14-M5.local (구현 호스트)
- 방식: production Supabase read-only SELECT + Mac mini SSH read-only (launchctl/plist/log). **DB row·status·queue·LaunchAgent 무변경.**
- selector: `budget-router-backfill-20260707-14-v1`
- 목표: source date 2026-07-07~2026-07-14 (8박) × 30 = **240**

## 1. 누적 진행률

| 항목 | 값 |
|---|---|
| 생성(created) | **90 / 240** (37.5%) |
| 미생성(not-created) | **150** (07-10~07-14, 5박) |
| succeeded | **60** |
| failed_terminal | 8 |
| failed_retryable | 3 |
| queued | 19 |
| held_model_mismatch | 0 |
| 성공률(판정된 것 기준, succeeded/(succeeded+terminal)) | 60/68 = 88.2% |

## 2. source date별

| source_date | created | succeeded | queued | retryable | terminal | held | 상태 |
|---|---|---|---|---|---|---|---|
| 2026-07-07 | 30 | 30 | 0 | 0 | 0 | 0 | ✅ 완료 (100%) |
| 2026-07-08 | 30 | 26 | 0 | 0 | 4 | 0 | ✅ 완료 (성공 26/30) |
| 2026-07-09 | 30 | 4 | 19 | 3 | 4 | 0 | ⏳ 진행 중 (open 22) |
| 2026-07-10 | 0 | – | – | – | – | – | ⬜ 미생성 |
| 2026-07-11 | 0 | – | – | – | – | – | ⬜ 미생성 |
| 2026-07-12 | 0 | – | – | – | – | – | ⬜ 미생성 |
| 2026-07-13 | 0 | – | – | – | – | – | ⬜ 미생성 |
| 2026-07-14 | 0 | – | – | – | – | – | ⬜ 미생성 |

- 완료 판정(next_source_date 계약): 한 night 이 `len(jobs)>=30 AND complete>=30`(complete=succeeded+failed_terminal) 이면 다음 night 로 진행. 07-07(30 succ), 07-08(26+4=30 complete) 완료, 07-09 는 22 open 이라 미완료.

## 3. 무결성 점검

| 점검 | 결과 |
|---|---|
| 정규↔backfill selector crossover (selector_version vs producer_run_id shape) | **0** |
| backfill selector 내 clip 중복 | **0** |
| held_model_mismatch | **0** |
| succeeded 의 non-exact model (≠claude-sonnet-5) | **0** |
| succeeded model_actual 관측치 | claude-sonnet-5 단일 |
| failure_diagnostic 컬럼 | 적용됨(migration OK) |
| backfill 실패 job 의 failure_diagnostic 채워짐 | 0 (실패가 diagnostic write 경로 배포 전 발생 — 신규 실패부터 채워질 예정) |

## 4. producer host / 정체

- backfill wave **생성** 위치: 07-07 = BaekBook-Pro-14-M5.local(MacBook, 초기), 07-08·07-09 = baeg-endeuui-Macmini.local. 이는 wave 생성 host 기록일 뿐, selector crossover 는 0 이라 queue 소유권 누출은 아님.
- 가장 오래된 open(queued/retryable) backfill job: 2026-07-15 12:13 UTC(KST 21:13) — 감사 시점 약 6.5h 경과. 전부 07-09 night. **야간이라 daytime guard(07~19 KST)로 대기 중 → 정상**(장애 아님).

## 5. 정규 candidate selector (참고, backfill 아님)

- 총 19 job: succeeded 15 + failed_terminal 4, open 0, held 0, 전부 claude-sonnet-5.
- **producer host 전부 MacBook(BaekBook-Pro-14-M5.local)** → 정규 후보 worker 가 Mac mini 가 아니라 MacBook 에서 실행돼 온 단일호스트 결함 확증(설계 §2.3). 이번 배포로 Mac mini 단일화.

## 6. LaunchAgent (Mac mini, read-only)

- `com.petcam.vlm-historical-backfill`: state=not running, last exit code=0.
- 현재 plist: **RunAtLoad=true + StartInterval=3600 (구 hourly)** — daytime calendar 재설치 미반영. 단, 배포된 worker 코드(95fa7e79)에 `backfill_allowed_now` guard 가 있어 off-hours cycle 은 no-op(방어 이중화). 배포 단계에서 07~19 calendar 로 재설치 예정.
- `com.petcam.vlm-backfill-finalizer`: 20:30 KST finalizer(별도 agent, 이번 범위 밖 — 건드리지 않음).
- 정규 candidate agent 는 Mac mini 에 **없음**(예상대로), MacBook 에 로드됨.

## 7. 예상 완료 (근거 있는 coarse 추정, 정밀 ETA 지양)

- 남은 작업: 07-09 open 22 + 미생성 5박(150) = 약 **5.7 night 상당 172 job**.
- 처리 제약: daytime guard 로 **07~19 KST(12h)만** 실행, cycle 당 미완료 night 하나를 진행(30/night), Claude 구독 한도 공유.
- 관측된 생성 이력: 07-07(07-15 12:25 KST) → 07-08(14:01) → 07-09(21:13)로 하루 1~2 night 페이스.
- **coarse 추정: 정상 daytime 진행 시 대략 3~6일. 실패 재시도·한도·speed 변동으로 폭 있음.** terminal 실패는 완료로 계산하지 않음(성공만 진척으로 집계).

## 8. 발견 이슈 요약

1. (정보) 정규 candidate 전량 MacBook 생성 — 배포로 Mac mini 단일화.
2. (정보) 07-07 backfill wave 만 MacBook 생성 — historical, crossover 아님.
3. (개선) Mac mini backfill plist 가 아직 구 hourly — worker guard 로 무해하나 daytime calendar 재설치 필요.
4. (관측성 공백) backfill 진행률을 알리는 Slack 메시지 부재 → 이번 작업으로 신설.
5. (무결성) crossover 0 / 중복 0 / held 0 / non-exact model 0 — 데이터 건전.

---

## 부록 — 진행률 의미 정정 + 실측 재확인 (2026-07-16 09:2x KST)

검수에서 "완료 78/240·남은 162" 표시가 worker 진행 계약과 불일치함이 발견돼 진행률 필드를 재정의했다.
- **처리 = succeeded + failed_terminal**(worker COMPLETE_STATUSES) · **남은 처리 = 240 − 처리**(영구실패 미포함) · **미생성 = 240 − created** · **진행 중 = queued+retryable+held+processing**.
- 실제 aggregate 코드를 live DB 로 실행해 formatter=DB 직접 대조(read-only):

| created | succeeded | terminal | in_progress | 처리 | 남은 처리 | 미생성 |
|--|--|--|--|--|--|--|
| 120 | 100 | 12 | 8 | **112/240** | **128** | 120 |

- ETA 는 succeeded 가 아니라 **남은 처리량** 기준. failed_terminal 12건은 재큐잉하지 않음(표시만 정정).
- 최종 메시지 계약: `처리 240/240 (성공 N · 영구실패 M) · 남은 처리 0`.
