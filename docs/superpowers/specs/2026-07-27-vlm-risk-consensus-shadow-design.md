# 위험 라벨 VLM consensus shadow 설계

**상태:** 방향 승인 완료, written spec owner review 대기
**승인:** 2026-07-27 owner — 위험 라벨만 3회 consensus하는 권장안 승인
**구현 레포:** `petcam-nightly-reporter`
**실행 호스트:** Mac mini `baeg-endeuui-Macmini.local`
**production provider:** `claude_cli_batch`, exact `claude-sonnet-5`

## 1. 목적

Mac mini VLM worker가 temperature를 제어할 수 없는 Claude CLI 단일 판정을 바로
production 결과로 확정하는 위험을 계량하고 줄인다.

이번 단계는 **shadow 측정만** 수행한다. 기존 첫 판정, job 상태, 앱·Slack·야간 리포트의
동작은 바꾸지 않는다. ROI, bbox, crop, prompt, selector를 추가하거나 수정하지 않는다.

## 2. 근거

`petcam-lab`의 visibility baseline reproduction은 과거 mismatch 44개를 현재
`v4.0 + six-768q85 + Sonnet 5 CLI` 계약으로 두 번 재실행했다.

- 두 회차 사이 label 변경: 10/44
- 두 회차 모두 같은 non-`moving`: 7/44
- 최종 3/3 stable error 상한: 7, 사전 gate 10 미만
- 결론: ROI가 무효라는 뜻이 아니라, 현재 ROI 투자 표적보다 판정 변동 통제가 우선

출처:

- `../petcam-lab/experiments/visibility-bbox-roi-20260727/REPORT.md`
- source HEAD `b56d5592fbcb1cc12431964dd992346e74730bd7`
- read-only cross-review verdict `CROSS_REVIEW_PASS`

44개는 historical error-selected set이므로 22.7%를 production 전체 변동률로
일반화하지 않는다. 이 결과는 shadow consensus를 측정할 근거이지 효과 수치가 아니다.

## 3. 검토한 접근

### A. 모든 clip을 3회 실행

- 장점: 전체 변동률을 직접 측정할 수 있다.
- 단점: 호출량과 처리시간이 거의 3배가 되고 정규 VLM·rolling backfill deadline을
  위협한다.
- 판정: 기각. fresh 운영 전량에 바로 적용하기에는 비용이 크다.

### B. 위험 결과가 나온 batch만 동일 조건으로 2회 추가 실행

- 장점: 기존 오탐 피해가 큰 결과를 집중 측정하고, 같은 batch·frame·prompt 계약을
  유지할 수 있다.
- 단점: 첫 결과가 `moving`이면 재실행하지 않으므로 전체 변동률과 false negative를
  직접 추정하지 못한다.
- 판정: 채택. precision-first shadow로 사용한다.

### C. temperature=0 직접 API 전환까지 대기

- 장점: 장기적으로 가장 명확한 결정론 계약이다.
- 단점: 유료 API 결제·운영 배선이 아직 준비되지 않아 즉시 적용할 수 없다.
- 판정: 장기 경로로 유지하되 이번 shadow를 막지 않는다.

## 4. 위험 라벨 계약

현행 7-class ontology에서 첫 판정이 `moving`이 아닌 경우를 위험 결과로 본다.

- `eating_paste`
- `eating_prey`
- `drinking`
- `shedding`
- `unseen`
- `hand_feeding`

이 집합은 care 의미 오판 또는 영상 부재 오판이 downstream 결과에 미치는 영향이 크기
때문에 선택한다. 새 class나 threshold를 만들지 않는다.

## 5. 실행 흐름

1. 기존 selector·job claim·R2 download·6-frame 추출을 그대로 수행한다.
2. 현재와 동일하게 최대 4 clips의 batch를 Claude CLI에 한 번 제출한다.
3. 첫 결과에 위험 라벨이 하나라도 있으면 **같은 ready batch 전체**를 같은 model,
   prompt, frame, clip 순서로 두 번 더 제출한다.
4. 세 결과를 shadow attempt 원장에 append-only로 기록한다.
5. 동일 clip의 action 세 개가 모두 같으면 `unanimous`, 아니면 `disagreed`로 집계한다.
6. shadow 단계에서는 첫 production 결과와 `clip_vlm_jobs.status/result`를 기존 방식대로
   유지한다. consensus가 앱·Slack·behavior·GT·selector에 영향을 주지 않는다.

위험 clip만 단독 재호출하지 않고 같은 batch 전체를 반복하는 이유는 batch 구성 차이로
attention 조건이 바뀌는 것을 막기 위해서다. 추가 다운로드나 frame extraction은 하지 않고
첫 batch의 임시 frame을 재사용한다.

한 clip이 위험 라벨을 만들면 같은 batch의 `moving` clip도 attempt 2·3에 포함한다. 이는
비용 산정과 batch attention 조건을 보존하기 위한 의도된 계약이다. 세 attempt 중 하나라도
clip 누락·중복·순서 변경·exact-model mismatch가 있으면 그 batch는 consensus로 계산하지
않고 명시적 integrity failure로 기록한다. shadow 단계에서는 2/3 다수결 결과도 만들지 않는다.

## 6. 저장 계약

내구성 있는 비교를 위해 `petcam-lab`의 별도 forward migration으로 append-only attempt
원장을 추가한다. 구체 schema는 구현계획에서 확정하되 다음 최소 계약을 지킨다.

- identity: `job_id + protocol_version + attempt_index`
- attempt index: 1, 2, 3
- 저장: action, confidence, exact model, 로컬에서 생성한 batch shadow-run identity의
  비가역 hash, token usage, 실행 시각, 성공·실패 코드
- 미저장: reasoning 원문, frame path, R2 key, signed URL, 사용자 식별정보
- 같은 attempt의 중복 write는 원자적으로 거부하거나 동일 payload일 때만 idempotent
- UPDATE/DELETE 금지, service role 전용, `search_path=''`

첫 production 결과도 attempt 1로 snapshot하되 기존 `clip_vlm_jobs` row를 재작성하지 않는다.
attempt 2·3 실패는 production job 실패로 승격하지 않고 shadow completeness로만 기록한다.

## 7. deadline·실패 처리

- 기존 batch deadline 660초 보호를 유지한다.
- 첫 production 판정이 끝난 뒤 남은 시간이 shadow 두 호출의 보수적 예산보다 작으면
  `shadow_deferred_deadline`으로 기록하고 추가 호출하지 않는다.
- 보수적 예산 계산식과 입력 표본은 구현계획에서 사전 등록한다. 실행 결과를 본 뒤 낮춰
  호출을 강행하지 않는다.
- auth, quota, exact-model mismatch, clip-set mismatch는 기존 breaker를 존중한다.
- shadow 호출 실패는 production 결과를 되돌리지 않는다.
- worker exit code는 원장 write 불일치·identity drift처럼 감사 무결성이 깨질 때만 nonzero다.
- 임시 media는 기존 `TemporaryDirectory` 안에서만 사용하고 종료 후 0이어야 한다.

## 8. 단계별 rollout

### S0 — 구현·로컬 검증

- consensus 판정 순수 함수와 append-only store adapter를 TDD로 구현
- feature flag 기본값 `false`
- 기존 worker byte-equivalent 경로와 전체 회귀 확인

### S1 — Mac mini shadow

- 첫 production 결과는 그대로 유지
- 위험 결과 batch만 2회 추가
- 앱·Slack·GT·behavior·selector write 0
- R2 download와 frame extraction 추가 0

### S2 — Owner GT 대조

fresh Owner GT 또는 이중 블라인드 합의 GT와 겹치는 clip만 별도 read-only 평가한다.
historical mismatch 44개는 회귀·진단용이며 adoption holdout으로 재사용하지 않는다.

### S3 — production 확정 규칙

S1·S2 gate 통과 후 별도 설계와 owner 승인을 받아야 한다. 이번 설계에서 켜지 않는다.

## 9. 측정 지표

- 위험 첫 판정 clip·batch 수
- 3/3 unanimous 수와 action별 비율
- disagreed 수와 transition 분포
- attempt 1 대비 consensus의 Owner GT precision
- true care event 보존율
- 추가 provider calls, token usage, wall time
- deadline defer 수, worker exit/error 변화
- 정규 VLM·backfill schedule 지연
- temp media 잔여

## 10. Shadow 완료 기준

다음을 모두 만족해야 S2 평가를 완료했다고 본다.

1. 위험 첫 판정 clip 100개 이상
2. 독립 camera-night 3개 이상, 카메라 2대 이상
3. Owner GT 또는 double-blind consensus와 겹치는 위험 clip 30개 이상
4. attempt 원장 completeness 100% 또는 명시적 deadline/auth/quota failure code
5. production 첫 결과·job 상태·앱·Slack·GT·behavior·selector mutation 0
6. 정규 VLM·backfill deadline miss 증가 0
7. worker exit/error 증가 0, temp media 0

표본이 부족하면 `HOLD_DATA`, schedule 또는 quota 영향을 넘으면 `REJECT_OPERATIONAL_COST`,
무결성 문제가 있으면 `REJECT_INTEGRITY`로 판정한다.

mutation 0은 실행 전후 row-count만 비교하지 않는다. 대상 job의 상태·결과와 관련
downstream 테이블의 식별 가능한 fingerprint를 비교하고, 허용된 append-only shadow
원장 증가만 별도로 설명한다.

## 11. Production 확정 후보 gate

S3를 제안하려면 fresh GT 교집합에서 다음을 모두 사전 등록해 다시 평가한다.

- 위험 라벨 false-action rate를 단일 첫 판정보다 상대 50% 이상 감소
- 3/3 규칙의 true care event 보존율 90% 이상
- 안정 오답은 별도 failure bucket으로 분리하고 consensus 성공으로 계산하지 않음
- 추가 호출량과 wall time이 Mac mini schedule gate를 통과
- 결과를 본 뒤 위험 class, 반복 수, 합격 숫자를 변경하지 않음

gate를 통과해도 불일치는 자동 `moving`으로 바꾸지 않는다. Owner 검수 또는
`review_candidate`로 보내는 별도 제품 계약이 필요하다.

## 12. 금지 경계

- ROI, bbox, crop, detector, prompt, threshold, selector 변경
- direct API 자동 전환 또는 새 비용 발생
- shadow 결과로 기존 production job 결과 덮어쓰기
- behavior log, app activity, 사람 GT, double-blind submission 수정
- 불일치를 자동 `moving`, `unseen`, skip으로 확정
- historical 44개를 fresh holdout 또는 production 정확도 근거로 사용
- owner 승인 전 LaunchAgent 변경·main merge·Mac mini 배포

## 13. 성공 후 장기 경로

shadow consensus는 영구적으로 3배 호출하는 최종 구조가 아니다.

1. 지금은 비결정성의 실제 운영 규모와 비용을 측정한다.
2. fresh GT로 위험 결과의 신뢰 계약을 만든다.
3. Python Evidence·Local VLM이 쉬운 영상을 처리하고 불확실한 영상만 cloud consensus로
   보내는 라우팅 근거로 사용한다.
4. temperature=0 provider가 준비되면 반복 호출을 줄이면서 같은 품질 gate를 유지한다.

최종 목표는 모든 영상을 분석하되, 비용을 통제하고 잘못된 care 행동이 운영 결과와
학습 데이터로 전파되지 않는 RBA 품질 계층이다.
