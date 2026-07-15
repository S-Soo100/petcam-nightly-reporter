# VLM 단일 호스트 운영 하드닝 설계

> 상태: 사용자 방향 승인, 구현 전 설계 정본
> 작성일: 2026-07-16
> 통합·대체 초안: `2026-07-15-claude-cli-batch-reliability-hardening-design.md`

## 1. 목적

밤 22·00·02·04시(KST), 직전 2시간마다 카메라별 최대 4개 후보만 Claude 구독 VLM으로 분석하는 서비스를 Mac mini 한 대에서 예측 가능하게 운영한다.

이번 하드닝은 다음 네 문제를 한 번에 해결한다.

1. 정규 후보 worker가 MacBook에 설치되어 있고 Mac mini에는 없는 배치 위치 오류
2. 정규 후보 worker와 역사 backfill worker가 서로의 queue를 처리하는 소유권 오류
3. `cli_rc_1`이 원인 불명으로 남아 실패와 회복 과정을 신뢰하기 어려운 관측 오류
4. Slack의 기존 두 상황판이 VLM을 실행하지 않으면서도 VLM 결과처럼 오해될 수 있는 운영 UX 오류

## 2. 확인된 현재 상태

### 2.1 Slack 메시지의 의미

- `라우터 메타 상황판`은 R2+OpenCV metadata 전용이며 LLM/VLM을 호출하지 않는다.
- `최근 30분 상황판`은 legacy activity reporter다. 현재 `SAMPLE_TOP_N=0`이므로 VLM 샘플링이 꺼져 있다.
- `샘플0: 특이행동 없음`은 분석 결과가 아니라 **분석하지 않음**인데 문구가 이를 숨긴다.
- 실제 정규 후보 VLM worker는 Slack을 보내지 않고 DB와 로컬 로그에만 결과를 남긴다.

### 2.2 실제 VLM 경로

- 스케줄: 22:00, 00:00, 02:00, 04:00 KST
- window: 직전 2시간
- 후보 슬롯: `customer_highlight`, `subtle_behavior`, `diversity_discovery`, `exclusion_audit`
- 최대 선택: window·camera당 4개, 기존 night cap 유지
- 입력: clip당 6 frame
- provider: `claude_cli_batch`
- exact model: `claude-sonnet-5`
- 직접 API 비용: 0원, Claude 구독 한도를 사용
- 금지: GT·앱 하이라이트·활동시간·Gate 자동 skip 변경

### 2.3 발견된 운영 결함

- 정규 candidate LaunchAgent는 MacBook에서 실행됐고 Mac mini에는 설치되지 않았다.
- MacBook 정규 worker의 초기 batch들은 terminal/retryable failure로 끝났다.
- Mac mini historical backfill worker는 정규 selector의 due job을 먼저 처리하도록 구현돼 있다.
- 정규 worker는 selector와 window를 제한하지 않은 전역 `load_due_jobs()`를 사용한다.
- 따라서 정규 worker와 backfill worker가 서로의 queue를 교차 소비한다.
- 코드와 시간 순서를 대조하면, MacBook이 실패시킨 정규 job을 약 한 시간 뒤 Mac mini backfill worker가 성공시킨 것으로 추론된다. 이 추론은 향후 producer host와 job timestamps로 재검증한다.

## 3. 목표 상태

```text
Mac mini 정규 scheduler
  └─ 2시간 window 후보 최대 4개 생성
      └─ 같은 selector + 같은 window job만 예약
          └─ Claude CLI batch, exact Sonnet 5
              ├─ DB 결과 저장
              └─ VLM 전용 Slack 요약 1회

Mac mini historical backfill scheduler
  └─ backfill selector job만 생성·예약

MacBook
  └─ 정규 candidate LaunchAgent 없음
```

## 4. 사용자 체험 설계

### 4.1 정상 실행

`[Slack] 사용자가 보는 것` → `22:00~00:00 후보 4개, 성공 4개, 행동 분포, 실제 모델, 비용 0원, host=Mac mini, 다음 실행 02:00`

`[사용자 판단]` → 후보가 왜 4개인지, 실제 VLM이 호출됐는지, 성공했는지 한 메시지로 확인한다.

`[감정]` → metadata 상황판과 VLM 분석을 혼동하지 않고 서비스를 믿을 수 있다.

### 4.2 후보 없음

`[Slack]` → `후보 0개 · VLM 호출 0회 · 정상 종료`를 표시한다.

`[사용자 판단]` → 장애가 아니라 해당 window에 분석 가치가 있는 후보가 없었다는 것을 안다.

### 4.3 Claude 실패

`[Slack]` → raw stderr 없이 `인증`, `일시 네트워크`, `응답 형식`, `모델 불일치` 같은 안전한 phase와 status count를 표시한다.

`[사용자 판단]` → 재시도 가능 여부와 다음 조치를 안다.

`[감정]` → 같은 실패가 무한 반복되거나 비밀값이 Slack에 노출될 걱정이 없다.

### 4.4 legacy activity 상황판

`[Slack]` → `샘플0: VLM 샘플링 꺼짐`으로 표시한다.

`[사용자 판단]` → `특이행동 없음`이라는 분석 결론으로 오해하지 않는다.

## 5. 핵심 불변조건

1. 정규 candidate worker의 production host는 Mac mini 한 대뿐이다.
2. 정규 worker는 정규 selector만, backfill worker는 backfill selector만 처리한다.
3. 정규 worker는 현재 window job을 오래된 recovery job보다 먼저 처리한다.
4. recovery는 같은 정규 selector 안에서만, 한 번에 최대 4개로 제한한다.
5. succeeded·failed_terminal·held_model_mismatch job은 다시 예약하지 않는다.
6. 동일 window의 중복 실행은 file lock과 DB idempotency로 중복 job을 만들지 않는다.
7. exact model이 `claude-sonnet-5`가 아니면 결과를 succeeded로 채택하지 않는다.
8. GT, `behavior_labels`, 앱 highlight, activity filter, effective activity를 쓰지 않는다.
9. Slack 실패는 VLM job 상태를 바꾸거나 Claude를 재호출하지 않는다.
10. raw stdout/stderr·이메일·token·전체 path·전체 UUID는 DB·Slack·일반 로그에 남기지 않는다.

## 6. 호스트 소유권

### 6.1 fail-closed host guard

정규 worker는 `VLM_EXPECTED_HOST`를 필수로 받는다. 실행 host가 일치하지 않으면 다음보다 먼저 nonzero로 종료한다.

- Supabase client 생성
- candidate 조회·run/job 생성
- R2 download
- Claude CLI 호출
- Slack 성공 요약

installer가 현재 host 값을 자동으로 expected host에 복사하면 MacBook 오설치가 자기 승인되므로 금지한다. 배포자가 Mac mini의 검증된 hostname을 명시해야 한다.

### 6.2 install guard

installer는 다음을 모두 검사한다.

- `VLM_EXPECTED_HOST` 미설정 → 설치 중단
- 실제 hostname 불일치 → 설치 중단
- provider가 `claude_cli_batch`가 아님 → production 모드 설치 중단
- model이 exact Sonnet 5가 아님 → 설치 중단
- plist lint 실패 → bootstrap 전 중단
- `HOME`, `USER`, `LOGNAME`, Claude 실행 PATH 누락 → 설치 중단

## 7. Queue 소유권

### 7.1 정규 worker

전역 `load_due_jobs()`를 사용하지 않는다. 다음 순서만 허용한다.

1. 현재 window의 run/job을 idempotent하게 생성한다.
2. `selector_version=정규`, `window_start>=start`, `window_start<end`, status=`queued|failed_retryable`만 조회한다.
3. 현재 window job을 처리한다.
4. 처리 후 현재 window의 `queued|failed_retryable`가 0개인지 다시 조회한다.
5. 현재 window가 terminal 상태가 된 뒤에만 정규 selector의 오래된 retryable job을 최대 4개 recovery 대상으로 허용한다.
6. backfill selector는 어떤 경우에도 읽지 않는다.

### 7.2 historical backfill worker

- 정규 selector 우선 처리 코드를 제거한다.
- `BACKFILL_SELECTOR_VERSION` job만 생성·조회·처리한다.
- 정규 야간 schedule과 shared Claude lock 경합을 피하려고 backfill은 07:00 이상 20:00 미만(KST)에만 시작한다.
- installer도 `RunAtLoad`·`StartInterval`을 제거하고 07~19시 정각의 명시 calendar schedule만 만든다.
- backfill 진행률이 끝날 때까지 별도 LaunchAgent로 유지할 수 있다.
- 완료 확인 전 LaunchAgent 삭제나 job 폐기를 하지 않는다.

정규 candidate가 scheduled 시각에 shared lock을 얻지 못하면 조용히 성공 종료하지 않는다. Claude는 호출하지 않고 `blocked_lock` VLM Slack 경고를 1회 보내며 nonzero로 끝낸다. backfill의 CLI 호출은 batch당 기존 300초 timeout을 유지하므로 정상 종료하지 않는 process가 무한히 lock을 잡지 않는다.

### 7.3 crash recovery

- run/job 생성 후 process가 죽으면 다음 정규 실행이 같은 selector의 retryable/queued를 bounded recovery한다.
- 현재 window가 우선이므로 오래된 queue 때문에 새 window가 굶지 않는다.
- sleep/offline으로 스케줄 자체가 실행되지 않은 window는 정규 worker가 임의 backfill하지 않는다. 아침 audit에서 missing window로 보고한다.

## 8. Claude CLI 실패 진단과 제한 재시도

기존 미커밋 신뢰성 초안의 유효 요구를 이번 설계에 흡수한다.

### 8.1 안전 diagnostic

forward migration으로 `clip_vlm_jobs.failure_diagnostic jsonb null`을 추가한다. 허용 필드:

- version
- phase: `auth|spawn|process|envelope|schema|clip_set|model|unknown`
- code
- exit_code
- redacted fingerprint
- allowlisted markers
- stdout_bytes, stderr_bytes
- provider_subattempts
- recovered

원문은 저장하지 않는다.

### 8.2 retry matrix

- 최대 provider subattempt: durable attempt당 2회
- 즉시 retry 가능: timeout, transient network, 분류 불가 `rc=1`
- 즉시 retry 금지: auth, quota, model mismatch, clip-set mismatch, envelope/schema/config 오류
- subattempt는 `attempt_count`를 추가 증가시키지 않는다.
- batch를 4→2→1로 자동 분할하지 않는다.
- 한 batch 실패가 이후 독립 camera batch를 불필요하게 중단시키지 않되, auth/quota/model breaker는 이후 호출을 중단한다.

## 9. Slack 관측 계약

정규 scheduled run마다 VLM 전용 메시지를 최대 1회 보낸다. 정상 후보 0개도 메시지를 보낸다.

필수 항목:

- window KST
- host
- 후보 수와 슬롯별 수
- `succeeded`, `failed_retryable`, `failed_terminal`, `held_model_mismatch`, `queued` 수
- 성공 결과의 action 분포
- provider와 `model_actual`
- model mismatch 수
- 직접 API 비용과 Claude 구독 모드 표기
- 가장 오래된 due job age와 30분 초과 여부
- 다음 정규 실행 시각
- 추적 가능한 짧은 run id

금지 항목:

- raw model reasoning
- raw stdout/stderr
- email, token, 전체 path, 전체 UUID
- “특이행동 없음”을 후보 0/분석 0의 의미로 사용

Slack webhook 실패 시 DB job은 이미 계산된 terminal 상태를 유지하고 Claude를 다시 호출하지 않는다. 일반 로그에 `slack=FAIL run=<short-id>`만 남긴다.

정확히 한 번 전송은 별도 durable outbox 없이는 보장하지 않는다. 이번 버전은 한 process 안에서 1회 호출하고 run id로 중복을 식별한다. process crash로 DB 성공 후 Slack이 누락될 수 있는 한계는 아침 audit에서 검출한다.

## 10. 오류·예외 처리

| 상황 | 동작 | 금지 |
|---|---|---|
| host mismatch | DB 접근 전 nonzero 종료 | 자동 self-approval |
| Mac mini offline/sleep | 해당 window missing으로 남김 | 다음 window에서 무제한 몰아 처리 |
| Supabase 조회 실패 | job 생성 전이면 종료, 생성 후면 recovery 가능 상태 보존 | 빈 후보로 성공 보고 |
| R2 download 실패 | 해당 job만 retryable/terminal 판정, batch 계속 | 임시 mp4 잔존 |
| frame 추출 실패 | 해당 job만 안전 code 저장 | raw path 저장 |
| Claude auth/quota | breaker, 같은 run 추가 호출 중단 | 즉시 반복 |
| transient/unknown rc1 | 동일 입력으로 1회만 subretry | 무한 retry |
| envelope/schema/clip set 오류 | batch 결과 미채택 | 부분 성공 가장 |
| model mismatch | held, Slack 경고 | succeeded 처리 |
| Slack 실패 | job 상태 불변, 로그 경고 | Claude 재호출 |
| candidate lock 경합 | Claude 0회, `blocked_lock` Slack, nonzero | 중복 run/job·조용한 run 누락 |
| backfill 야간 실행 요청 | DB/R2/Claude 전 no-op | 정규 schedule과 lock 경합 |
| 오래된 regular job | current window 후 bounded recovery | backfill worker가 대신 처리 |
| backfill job | backfill worker만 처리 | 정규 worker 소비 |

## 11. 배포 순서

구현과 production 변경을 분리한다.

1. 코드·테스트·문서 구현
2. 전체 테스트와 build-equivalent 검증
3. 사용자 검토 후 commit/push
4. 별도 승인 후 forward migration 적용
5. Mac mini repo main 동기화와 launchd-equivalent preflight
6. 정규 worker가 실행 중이지 않은 시각에 MacBook LaunchAgent bootout
7. Mac mini installer dry render·plist 검증
8. Mac mini에 정규 LaunchAgent 설치
9. 다음 한 window canary
10. DB·로그·Slack·임시파일·producer host 검증 후 유지

MacBook bootout과 Mac mini bootstrap 사이에는 두 호스트가 동시에 실행되지 않게 한다. 역사 backfill은 queue 격리 적용 후 backfill selector 전용으로만 유지한다.

## 12. 수용 기준

한 개의 관측 night에서 다음을 모두 만족해야 한다.

- 22·00·02·04시 정규 run 4개 존재
- 모든 정규 run의 producer host가 Mac mini
- camera·window당 후보 최대 4개, active camera 4대 기준 window당 최대 16개
- night 전체 최대 64개
- 정규 job에서 backfill selector 처리 0개
- backfill worker에서 정규 selector 처리 0개
- `model_actual != claude-sonnet-5` 0개
- terminal 처리되지 않고 30분 넘은 정규 job 0개
- 원인 없는 `cli_rc_1` 0개
- scheduled run별 VLM Slack 요약 누락 0개(관측 night 기준)
- scheduled candidate의 `blocked_lock` 0개
- GT/app/highlight/activity 관련 row 변경 0개
- 임시 mp4/frame 잔존 0개

## 13. 범위 밖

- 후보 selector 정책·slot 수 변경
- static clip 전량 VLM 분석
- Gate v3 재학습
- 앱 활동시간 계산 변경
- VLM 결과 자동 GT 채택
- 하이라이트 자동 등록
- 직접 Anthropic API 활성화
- 외부 host-down 모니터링/정확히 한 번 Slack outbox

## 14. 기존 미커밋 초안 처리

`2026-07-15-claude-cli-batch-reliability-hardening-design.md`와 동명 plan은 삭제하거나 수정하지 않는다. 이번 문서와 구현 계획이 그 초안의 안전 진단·retry 요구를 흡수해 통합 대체한다. 구현자는 두 plan을 연속 실행하지 않고 2026-07-16 plan만 정본으로 사용한다.
