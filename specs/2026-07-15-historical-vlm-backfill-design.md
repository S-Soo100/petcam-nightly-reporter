# Historical VLM Backfill 240 Design

**상태:** 승인됨 — 구현 계획 전 설계 정본  
**작성일:** 2026-07-15  
**목표:** 2026-07-07 밤부터 2026-07-14 밤까지의 과거 영상에서 하루 30개씩 총 240개를 오늘 8시간 동안 Claude Sonnet 5로 shadow 분석한다.

## 1. 범위

### 포함

- source night 8개:
  - 2026-07-07 20:00 KST ~ 2026-07-08 04:00 KST
  - 이후 하루씩 증가
  - 2026-07-14 20:00 KST ~ 2026-07-15 04:00 KST
- source night당 Claude 분석 성공 목표 30개, 총 240개.
- 오늘 한 시간에 source night 하나씩, 총 8 wave로 실행한다.
- provider는 `claude_cli_batch`, exact model은 `claude-sonnet-5`다.
- clip당 6 JPEG, Claude 호출당 최대 4 clip을 유지한다.
- VLM 결과와 backfill용 Gate provenance는 기존 `clip_vlm_selector_runs`·`clip_vlm_jobs`에만 저장한다.

### 제외

- `behavior_logs`, `camera_clips`, 앱 하이라이트, 활동시간, 사람 GT를 변경하지 않는다.
- Gate 판정을 행동 GT나 자동 삭제·hard skip 근거로 쓰지 않는다.
- `exclude_absent`·`exclude_static` 스위치를 변경하지 않는다.
- 기존 정규 22·00·02·04시 VLM LaunchAgent의 설정·일정을 바꾸지 않는다.
- DB migration이나 새 테이블을 추가하지 않는다.

## 2. 현재 사실과 설계 근거

- 2026-07-07~07-14 KST의 `motion_clips`는 8,059개다.
- 그중 7,988개는 현재 주 카메라에서 생성됐다.
- 이 과거 구간의 기존 VLM job은 0개다.
- 과거 구간의 `activity-v1` evidence coverage는 17.2%뿐이다.
- 따라서 기존 네 슬롯을 raw metadata에 바로 적용하지 않고, 제한된 local Gate 사전검사로 후보 evidence를 보강한다.
- Gate의 absent recall은 아직 adoption 기준을 통과하지 못했으므로, Gate 결과는 후보 구성과 감사 표본에만 사용한다.

## 3. 후보 생성

### 3.1 source night 시간 균형

각 source night를 다음 8개 한 시간 bucket으로 나눈다.

`20~21`, `21~22`, `22~23`, `23~00`, `00~01`, `01~02`, `02~03`, `03~04`

source night index를 `i=0..7`, 시간 bucket index를 `b=0..7`로 둔다. `b=i`와 `b=(i+4) mod 8`에서는 3개, 나머지 6개 bucket에서는 4개를 뽑아 night당 정확히 30개를 만든다. 첫 3개 bucket은 `diversity`, 두 번째 3개 bucket은 `exclusion_audit` 슬롯을 생략한다. 따라서 night마다 슬롯 quota는 `8/8/7/7`이고, 8개 night 전체에서는 각 시간대가 정확히 30개씩 선택된다.

### 3.2 local Gate prepool

- 각 한 시간 bucket에서 녹화 시각과 `motion_score` 분위수를 함께 사용해 최대 15개를 prepool로 만든다.
- source night당 최대 120개만 R2에서 받아 기존 `activity-v1` evidence를 재사용하거나 local Gate evidence를 메모리에서 계산한다.
- 동일한 30분 episode에서 유사 clip이 반복되면 대표 하나를 우선한다.
- Gate는 `presence`, `activity`, bbox, confidence, motion evidence를 제공하지만 어떤 clip도 자동 탈락시키지 않는다.
- 새로 계산한 Gate evidence는 `clip_prelabels`·`clip_activity_assessments`에 쓰지 않는다. 최종 선택된 30개의 evidence snapshot만 VLM job `rank_features`에 남겨 앱의 과거 활동시간이 바뀌지 않게 한다.

### 3.3 Claude 후보 30개

기존 네 의도를 source night 전체에서 균형 있게 유지한다.

- `highlight`: 큰 움직임이나 눈에 띄는 변화 8개
- `subtle_behavior`: 낮은 motion이지만 게코 evidence가 있거나 미세행동 가능성이 있는 영상 8개
- `diversity`: 시간대·위치·motion 분위수·evidence 조합이 덜 반복된 영상 7개
- `exclusion_audit`: Gate가 absent/static/unknown으로 본 영상의 안전성 감사 7개

한 clip은 한 슬롯에만 들어간다. 이미 동일 model·prompt·sampler로 성공한 clip은 selector version과 관계없이 제외한다. 후보가 부족하면 같은 source night의 다른 bucket과 슬롯에서 채우되 episode 중복보다 시간대 균형을 우선한다.

## 4. DB와 배치 경계

- backfill selector version은 정규 selector와 구분되는 고정 버전을 사용한다.
- 기존 RPC의 run당 최대 4 job 계약을 유지한다.
- 한 시간 bucket당 selector run 하나를 만들고 3~4 job만 넣는다.
- source night 하나는 8개 selector run, Claude batch 최대 8회로 구성된다.
- 8개 source night 전체는 64개 이하의 Claude batch다.
- `clip_vlm_jobs`의 기존 identity와 상태를 재사용해 재실행 시 중복 job·중복 분석을 막는다.

## 5. 시간당 실행과 재개

- 전용 임시 LaunchAgent가 오늘만 1시간 간격으로 최대 8회 실행된다.
- 매 실행은 DB에서 가장 이른 미완료 source night 하나를 결정한다.
- 첫 실행은 2026-07-07 밤, 마지막 실행은 2026-07-14 밤이다.
- wave 시작 시 selector run/job 30개를 durable하게 만든 뒤 Claude 분석을 시작한다.
- 개별 R2/프레임 오류는 같은 wave 안에서 최대 1회 재시도한다.
- 프로세스가 중단돼도 다음 실행은 DB 상태를 읽고 queued/failed_retryable job부터 이어간다.
- 30개가 성공 또는 terminal 상태가 되어야 다음 source night로 넘어간다. terminal 실패는 최종 보고에 별도 표시한다.
- 8개 source night가 끝나면 임시 LaunchAgent는 더 이상 분석하지 않고 정상 no-op한다. 검증 후 plist를 제거한다.

## 6. 동시 실행과 안전 중단

- 정규 VLM worker와 동일한 file lock을 사용해 Claude 호출이 겹치지 않게 한다.
- 정규 22·00·02·04시 job이 존재하면 정규 job을 먼저 처리하고 backfill은 다음 기회로 미룬다.
- 다음 조건에서는 현재 wave와 이후 wave를 멈춘다.
  - Claude CLI 인증 실패
  - 구독/session limit 또는 quota 신호
  - 요청 모델과 실제 모델 불일치
  - batch 응답의 clip ID 집합 불일치
  - 연속 provider 실패
- 안전 중단 시 기존 성공 결과는 보존하고, 미완료 job은 재현 가능한 상태로 남긴다.
- 로그에는 토큰·상태·안전한 오류코드만 기록하고 이메일·토큰·키는 기록하지 않는다.

## 7. 비용·한도

- Claude Code 구독 CLI를 사용하므로 DB의 실제 API 비용은 `$0`이다.
- 첫 8개 실측을 단순 환산하면 240개의 API 환산 참고비용은 약 `$35`다.
- 이 값은 실제 청구액이 아니라 동일 사용량을 API로 호출했을 때의 비교값이다.
- wave마다 input/cache/output token과 환산비용을 누적해 구독 한도 효율을 검토한다.

## 8. 관측과 보고

각 wave 종료 시 다음을 기록한다.

- source night, clips seen, Gate prepool 수
- 선택 30개의 시간 bucket·슬롯 분포
- succeeded/failed/held 수와 attempt count
- 행동 판정·confidence 분포
- 요청/실제 모델 일치 여부
- token 합계와 API 환산 참고비용
- 임시 MP4 잔존 수

최종 보고에는 240개 job 목록, 날짜별 요약, 행동 분포, 실패/재시도, 후보 썸네일 또는 contact sheet 위치, 실제 비용 `$0`, API 환산 참고비용을 포함한다.

## 9. 검증과 완료 조건

- selector 단위 테스트: night당 정확히 30개, 8-night 총 240개, 시간대 균형, 슬롯 quota, episode 중복 억제.
- resume 테스트: 동일 wave 재실행 시 새 job·Claude 중복 호출 0.
- safety 테스트: auth/quota/model mismatch에서 이후 wave 0.
- integration smoke: source night 하나를 dry-run해 선택 목록만 출력하고 DB/Claude write 0.
- production canary: 첫 wave 30개 처리 후 DB·모델·비용·임시파일을 검증한다.
- 최종 완료:
  - 8개 source night 각각 30 job 생성
  - 총 240개가 succeeded 또는 명시적 terminal 상태
  - 요청/실제 모델 불일치 0
  - 앱·GT·활동시간 write 0
  - 임시 MP4 잔존 0
  - SOT와 운영 보고서 갱신

## 10. 롤백

- 임시 LaunchAgent를 `bootout`해 이후 wave를 즉시 중단한다.
- backfill selector run/job은 감사 원장이므로 삭제하지 않는다.
- 정규 VLM LaunchAgent와 activity worker는 계속 유지한다.
- backfill 결과는 shadow-only라 앱 롤백이나 DB 데이터 복구가 필요하지 않다.
