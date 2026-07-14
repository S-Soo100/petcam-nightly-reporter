# 예산 고정형 Claude VLM 후보 라우터 v1 설계

> 상태: 설계 승인 / 구현 전
> 작성일: 2026-07-14
> 적용 레포: `petcam-nightly-reporter`(라우터·분석기), `petcam-lab`(forward DB migration·SOT)

## 1. 한 줄 결정

밤 20~04시를 2시간 구간으로 나눠 22·00·02·04시 KST에 네 번 실행한다. 각 카메라·구간에서 Claude VLM 후보를 최대 4개만 고르되, `하이라이트 / 미세행동 / 다양성 / 제외감사` 슬롯을 각각 1개로 분리한다. 후보가 없으면 빈 슬롯을 유지하며 Gate의 `absent/static/unknown`만으로 clip을 영구 폐기하지 않는다.

## 2. 배경과 문제

현재 nightly 행동 분류기는 다음 구조다.

- 30분마다 밤중 실행
- 구간별 `motion_score` 상위 1개를 `claude -p`로 분류
- 6~20장의 최대 1080px 프레임을 Claude Code `Read` 도구로 열람
- v4.0 분류 프롬프트를 Claude Code 기본 시스템 프롬프트 뒤에 append
- 실측 약 12만 token/clip, API 환산 약 `$0.44/clip`
- informative 결과를 `camera_clips + behavior_logs`에 자동 등록

이 방식은 세 문제가 있다.

1. **입력량 문제:** 카메라가 1대에서 곧 4대로 늘면 전량 분석은 구독 한도와 API 비용 모두 감당할 수 없다.
2. **선정 편향:** `motion_score` 상위만 고르면 큰 일반 이동이 대부분을 차지하고 음수·급여·탈피 같은 낮은 움직임 행동이 누락된다.
3. **신뢰 경계:** Gate v2 `exclude_absent`는 실제 active clip을 놓친 사례가 있고 `exclude_static`도 독립 에피소드 검증이 부족하다. 이를 hard skip으로 사용하면 중요한 영상을 조용히 버릴 수 있다.

추가로 현재 코드는 CLI에 `--model sonnet` 최신 별칭을 전달하면서 provenance는 `claude-sonnet-4-6`으로 고정한다. 별칭이 새 모델로 이동하면 실제 모델과 기록이 달라진다. 재가동 전에 반드시 해소한다.

## 3. 목표

1. clip 유입량과 무관하게 카메라별 Claude 최대 호출량을 예측 가능하게 고정한다.
2. 고객에게 보여줄 가치가 있는 행동과 향후 모델 학습용 다양성 데이터를 동시에 수집한다.
3. Gate/activity evidence를 후보 우선순위에 활용하되 검증되지 않은 판정을 영구 제외 근거로 쓰지 않는다.
4. 후보 선정·모델·프롬프트·frame sampler·token·비용을 재현 가능한 형태로 남긴다.
5. 자동 등록 전 3일 shadow와 사람 검수로 후보 품질과 직접 이미지 입력 품질을 검증한다.

## 4. 범위

### In

- 22·00·02·04시 KST 네 번, 직전 2시간 구간 후보 선정
- 카메라당 구간별 최대 4개 후보
- 시간·activity/Gate evidence 기반 episode dedup
- 네 개의 독립 후보 슬롯
- durable DB job/결과 기록과 멱등성
- Anthropic Messages API 직접 이미지 입력
- exact model provenance, token/cost ledger, circuit breaker
- 3일 shadow 및 사람 검수용 리포트

### Out

- 원본 `motion_clips`/R2 삭제 또는 보존 기간 변경
- capture sensitivity 변경
- Gate `absent/static` 단독 hard skip
- Gate 결과로 행동 확정 또는 VLM 호출 전체 차단
- 결과의 `behavior_logs`, 앱 하이라이트, 고객 알림 자동 등록
- Flutter 화면 변경
- VLM fine-tuning, Gate v3 학습·배포
- 카메라 4대가 실제 연결되기 전 임의 UUID 등록

## 5. 사용자 체험

### 5.1 고객 관점

1. `[화면]` 다음 날 앱에서 밤중 활동시간과 선별된 행동·하이라이트 후보를 본다.
2. `[조작]` 고객이 하이라이트를 열어 원본 영상을 재생한다.
3. `[반응]` 연속된 거의 같은 이동 영상 대신 시간대별 대표 활동과 의미 행동 후보가 보인다.
4. `[감정]` “많이 찍혔다”가 아니라 “밤에 무엇을 했는지 볼 수 있다”고 느낀다.

v1 shadow에서는 이 고객 노출을 실제로 변경하지 않는다. 사람 검수 통과 후 별도 승격한다.

### 5.2 owner/연구 관점

1. `[화면]` 매 구간의 네 슬롯, 선택 이유, evidence, Claude 결과, 비용을 한 묶음으로 확인한다.
2. `[조작]` owner가 선택 후보와 함께 무선택·제외감사 표본을 직접 재생해 잘못 거른 영상을 확인한다.
3. `[반응]` false exclusion은 Gate v3 hardcase로, 희귀·새 행동은 GT 수집 후보로 분리된다.
4. `[감정]` Claude 한도를 무작정 소모하지 않고 “무엇을 왜 분석했는지” 설명할 수 있다.

## 6. 전체 데이터 흐름

```text
motion_clips (직전 2시간, 카메라별)
  + clip_prelabels
  + clip_activity_assessments(activity-v1)
  + 기존 VLM/job 이력
        ↓
기본 eligibility 검사
        ↓
저비용 episode dedup
        ↓
네 슬롯별 독립 pool 구성·순위화
        ↓
clip_vlm_selector_runs INSERT + 최대 4개 clip_vlm_jobs INSERT (queued)
        ↓
호출량·월 비용 budget guard
        ↓
개별 고해상 frame bundle → Anthropic Messages API
        ↓
result + 실제 model + usage + cost 저장
        ↓
shadow 사람 검수 / Gate v3 hardcase / 향후 고객 승격
```

## 7. 실행 구간과 호출 상한

### 7.1 스케줄

| 트리거 | 분석 구간(KST) |
|---|---|
| 22:00 | 20:00~22:00 |
| 00:00 | 22:00~00:00 |
| 02:00 | 00:00~02:00 |
| 04:00 | 02:00~04:00 |

04:00~06:00은 v1의 네 번 제한에서 의도적으로 제외한다. 실제 데이터에서 이 구간의 희귀 행동 손실이 확인되면 기존 네 슬롯의 시간을 재배치하며 다섯 번째 실행을 자동 추가하지 않는다.

### 7.2 상한

- 카메라·구간: 최대 4개
- 카메라·밤: 최대 16개
- 현재 1대: 최대 16개/밤
- 향후 4대: 최대 64개/밤
- 전체 밤 hard cap: 64개
- 적합 후보가 없으면 빈 슬롯 유지
- 월 API hard cap 초기값: `$10`
- 비용 원장 조회 실패 또는 예상비용 예약 후 `$10`을 넘으면 신규 API 호출 없이 job을 `held_budget`으로 보존
- 월 cap 증액은 별도 사용자 승인 없이는 금지

`64개/밤`은 후보 상한이지 비용 cap을 우회하는 보장 처리량이 아니다. 4대에서 비용 cap에 닿으면 모든 카메라가 최소 한 번씩 기회를 갖도록 camera round-robin 후 슬롯 순서로 처리한다.

## 8. 기본 eligibility와 hard skip

hard skip은 다음 네 경우로 제한한다.

1. `duration_sec <= 0`, `r2_key` 없음처럼 분석 입력 자체가 무효
2. 같은 `clip_id + selector_version` job이 이미 queued/submitted/succeeded
3. 같은 prompt/model/sampler로 성공한 VLM 결과가 이미 존재
4. episode 안에서 다른 clip이 같은 슬롯의 대표로 선택됨

4번은 영구 폐기가 아니다. 선택되지 않은 clip은 원본으로 남고 새 selector/model에서 다시 후보가 될 수 있다.

다음은 hard skip 사유가 아니다.

- `gecko_visible=false`
- `decision=exclude_absent`
- `decision=exclude_static`
- `decision=unknown`
- 낮은 `motion_score`
- 동일 모프·동일 카메라

## 9. 저비용 episode dedup

VLM 호출 전에 영상을 다시 다운로드해 embedding을 만들지 않는다. v1은 이미 저장된 메타/evidence만 사용한다.

같은 카메라의 clip을 시작시각 순으로 보고 다음 조건을 모두 만족하면 같은 episode로 묶는다.

- 이전 clip과 시작시각 간격이 120초 이하
- `activity-v1 decision`이 동일
- `motion_score`가 같은 카메라·구간 내 사분위 bucket에 속함
- Gate bbox가 있으면 중심점 3×3 grid와 크기 bucket이 동일; 둘 다 bbox가 없으면 이 조건은 동일로 취급

episode별 대표는 슬롯마다 다시 고르지 않는다. 먼저 episode 대표 1개를 정하고, 그 clip만 네 슬롯 pool에 진입시켜 한 episode가 여러 슬롯을 점유하지 못하게 한다.

대표 선택 우선순위는 `evidence reliability → clip duration 유효성 → 슬롯 공통 novelty → started_at 중앙값 근접`이다. 동률은 `sha256(camera_id + window_start + clip_id)`로 결정해 재실행 결과를 고정한다.

## 10. 네 후보 슬롯

하나의 종합점수로 상위 4개를 뽑지 않는다. 각 슬롯은 독립 pool과 목적을 가진다. 앞 슬롯에서 선택된 clip은 뒤 슬롯 pool에서 제거한다.

### 10.1 `customer_highlight`

목적: 고객이 보고 싶어 할 큰 활동·탐색·놀이 후보.

우선순위 evidence:

- `decision=active`
- Gate gecko visible 또는 visibility uncertain이지만 bbox/ROI evidence 존재
- gecko ROI flow와 active ratio가 구간 내 상위 사분위
- bbox 위치 변화 또는 지속적 ROI 움직임
- 해당 시간대·bbox grid가 최근 7일에 덜 선택됨

`motion_score`는 동점 보조값일 뿐 단독 1순위가 아니다.

### 10.2 `subtle_behavior`

목적: 음수·급여·탈피처럼 global motion은 작지만 게코 주변에 국소 변화가 있는 후보.

우선순위 evidence:

- Gate bbox/ROI evidence 존재
- global flow는 구간 중앙값 이하
- gecko ROI flow 또는 ROI/global flow 비율은 구간 중앙값 이상
- 같은 자리에 머무르면서 국소 변화가 반복됨
- 과거 `moving` 위주 샘플과 다른 motion bucket

v1은 머리·혀 위치를 확정하지 않는다. “머리/혀 행동”이라는 라벨을 local evidence로 만들지 않고, **bbox 내부 국소 변화 후보**로만 Claude에 전달한다.

### 10.3 `diversity_discovery`

목적: 현재 분포가 적은 시간·자세·환경·활동 상태를 수집해 GT와 future model 데이터를 넓힌다.

최근 7일 선택 이력에서 다음 bucket 조합의 빈도가 낮을수록 우선한다.

- camera
- 2시간 time bucket
- activity decision
- motion_score 사분위
- bbox 3×3 위치 grid
- bbox 크기 small/medium/large/none

별도 embedding 모델은 도입하지 않는다. 상위 희소 bucket 세 개 중 deterministic weighted choice로 하나를 골라 항상 똑같은 극단값만 선택되는 문제를 막는다.

### 10.4 `exclusion_audit`

목적: 분석하지 않을 후보에 실제 활동·희귀 행동이 숨어 있는지 지속 감시한다.

- pool: `exclude_absent / exclude_static / unknown`
- 같은 상태만 반복되지 않도록 window별 round-robin
- 현재 static canary 카메라는 `exclude_static`을 감사 pool에서 제외하지 않음
- detector active false exclusion 사례가 있는 `exclude_absent`도 계속 표본화
- 동률은 deterministic random으로 선택

이 슬롯 결과는 product exclude GT가 아니다. 사람 확인 전 Gate presence 학습 GT로도 사용하지 않는다.

## 11. DB 계약

forward migration으로 `public.clip_vlm_selector_runs`와 `public.clip_vlm_jobs`를 추가한다. 원본 activity migration은 수정하지 않는다.

### 11.1 `clip_vlm_selector_runs`

후보가 0개인 구간도 실행 사실과 제외 분포를 남긴다.

| 그룹 | 필드 |
|---|---|
| identity | `id`, `camera_id`, `window_start`, `window_end`, `selector_version` |
| input summary | `clips_seen`, `hard_invalid_count`, `already_processed_count`, `episode_count` |
| pool summary | `pool_counts jsonb`, `selected_clip_ids jsonb`, `unselected_reason_counts jsonb` |
| budget snapshot | `monthly_budget_usd`, `month_reserved_usd`, `month_actual_usd` |
| producer | `producer_host`, `producer_run_id`, `created_at`, `completed_at` |

멱등 제약:

```sql
unique (camera_id, window_start, selector_version)
```

### 11.2 `clip_vlm_jobs`

필수 필드:

| 그룹 | 필드 |
|---|---|
| identity | `id`, `selector_run_id`, `clip_id`, `camera_id`, `window_start`, `window_end`, `slot`, `selector_version` |
| selection snapshot | `episode_key`, `rank_features jsonb`, `selection_reason`, `activity_assessment_id`, `prelabel_id` |
| lifecycle | `status`, `attempt_count`, `queued_at`, `submitted_at`, `completed_at` |
| analyzer provenance | `model_requested`, `model_actual`, `prompt_version`, `prompt_sha256`, `sampler_version`, `frames_sampled` |
| provider | `provider_request_id`, `result jsonb`, `error_code` |
| usage | `reserved_cost_usd`, `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`, `cost_usd`, `pricing_version` |
| producer | `producer_host`, `producer_run_id`, `created_at` |

`status`는 `queued / submitted / succeeded / failed_retryable / failed_terminal / held_budget / held_model_mismatch`로 제한한다.

멱등 제약:

```sql
unique (clip_id, selector_version)
unique (selector_run_id, slot)
```

`selector_run_id`는 `clip_vlm_selector_runs(id) on delete cascade`다. DB에는 run을 먼저 저장하고 같은 transaction에서 최대 4개 job을 생성한다. durable job commit이 끝나기 전에는 API를 호출하지 않는다.

두 테이블의 RLS는 activity 테이블과 같은 계약을 사용한다.

- service_role만 insert/update
- authenticated owner는 자기 `motion_clips.owner_id`에 속한 row만 select
- anon/authenticated write policy 0개
- run의 camera FK는 `cameras(id) on delete cascade`
- job의 clip FK는 `motion_clips(id) on delete cascade`

API 비밀값, base64 이미지, 원본 프레임은 DB에 저장하지 않는다.

## 12. Claude 입력·모델 계약

### 12.1 Claude Code CLI를 운영 VLM으로 사용하지 않는다

운영 analyzer는 Anthropic Messages API를 직접 호출한다. Claude Code의 기본 agent prompt와 `Read` tool 왕복을 제거해 분류에 필요한 이미지·텍스트만 보낸다.

### 12.2 이미지

- 개별 JPEG 6장
- 시간순
- 긴 변 768px, no-upscale
- JPEG quality 85
- contact sheet는 사용하지 않음
- 기존 frames-beat-montage 근거를 유지
- 입력 순서: images → 짧은 clip metadata → 분류 질문

### 12.3 출력

JSON schema:

```json
{
  "type": "object",
  "properties": {
    "action": {
      "type": "string",
      "enum": ["eating_paste", "eating_prey", "drinking", "shedding", "moving", "unseen", "hand_feeding"]
    },
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "reasoning": {"type": "string", "maxLength": 300}
  },
  "required": ["action", "confidence", "reasoning"],
  "additionalProperties": false
}
```

- `max_tokens=256`
- temperature를 API가 지원하는 범위에서 0으로 고정
- system prompt v4.0은 cache breakpoint 적용
- 긴 자유 서사와 tool 사용 금지

### 12.4 모델 provenance

- `sonnet` 같은 alias 금지
- `ANTHROPIC_MODEL`은 exact model ID만 허용
- 요청값은 `model_requested`, 응답의 실제값은 `model_actual`에 각각 저장
- 두 값이 다르면 결과는 저장하되 product publish 금지, 이후 호출은 `held_model_mismatch`
- 현재 v4.0/4.6 평가와 다른 모델을 사용하면 30개 사람 GT 비교를 통과하기 전 baseline 교체를 주장하지 않음

## 13. 비용·한도 가드

### 13.1 비용 원장

각 응답 usage로 실제 비용을 계산한다. 계산식과 가격표 버전은 코드 상수 한 곳에 두고 `pricing_version`을 `rank_features`가 아닌 analyzer provenance에 기록한다.

API 호출 전에는 보수적 예상비용을 월 budget에 예약하고, 완료 뒤 실제비용으로 정산한다. 프로세스가 죽어도 submitted job의 예약비용은 해제하지 않으며 terminal 결과를 확인한 뒤에만 정산한다.

### 13.2 circuit breaker

다음 조건은 즉시 신규 호출을 멈춘다.

- 월 `$10` cap 도달 또는 원장 조회 실패
- 인증·결제 오류
- model mismatch
- 연속 retryable 실패 3건
- response usage 또는 result schema 누락

rate limit·5xx·연결 오류만 최대 2회 지수 backoff 재시도한다. 인증·validation·budget 오류는 재시도하지 않는다.

### 13.3 Batch API

v1 shadow는 결과 지연과 디버깅 변수를 줄이기 위해 동기 Messages API로 시작한다. 3일 실측에서 월 예상비용이 `$10`을 넘을 때만 `diversity_discovery`와 `exclusion_audit` 두 슬롯을 Batch API로 옮긴다. 고객·미세행동 슬롯은 2시간 결과 cadence를 유지한다.

## 14. 저장·승격 경계

shadow 동안:

- `clip_vlm_jobs.result`에만 저장
- `REGISTER_HIGHLIGHTS=0`
- `behavior_logs` write 없음
- 앱 하이라이트·리포트·활동시간 변경 없음
- Slack에는 성공/실패/비용/slot 분포만 owner 운영 로그로 표시

사람 검수와 비용 기준을 통과해도 자동 승격하지 않는다. 사용자 별도 승인 뒤에만 고객 슬롯 결과의 downstream 등록 설계를 연다.

## 15. 실패 처리

- 후보 selector 일부 row 오류: 해당 clip만 제외하고 window 계속, 오류 count 기록
- activity evidence 없음: hard skip하지 않고 `diversity_discovery` 또는 `exclusion_audit`의 unknown 후보가 될 수 있음
- R2 다운로드/프레임 추출 실패: temp 정리, `failed_retryable` 또는 terminal 분류
- 한 clip 실패: 다른 세 슬롯과 다른 카메라 계속
- DB job INSERT 실패: API 호출 금지. durable job 없이 과금하지 않음
- API 성공 후 DB 저장 실패: provider request ID를 로그에 남기고 같은 idempotency identity로 복구; 무조건 재호출 금지
- process overlap: window advisory lock 또는 동일 unique 제약으로 두 번째 실행이 0 job 생성

## 16. 검증 계획

### 16.1 selector offline replay

API 호출 없이 기존 3개 야간 window를 재생한다.

통과 조건:

- 같은 입력·selector version이면 같은 clip 선택
- 한 episode가 두 슬롯을 점유하지 않음
- eligible pool이 있으면 audit 슬롯 1개 생성
- `motion_score` 상위 moving이 네 슬롯을 전부 차지하지 않음
- Gate absent/static은 hard skip되지 않음
- 카메라·window당 4개, 전체 밤 64개 상한을 넘지 않음

### 16.2 direct API 품질·토큰 30개

사람 GT가 있는 다양한 30개를 prompt 변경 없이 동결하고 기존 Claude CLI 결과와 직접 API 결과를 비교한다.

통과 조건:

- median total input token `<= 12,000/clip`
- p95 total input token `<= 20,000/clip`
- exact action accuracy가 기존 CLI보다 3%p 넘게 하락하지 않음
- drinking/feeding/shedding 합산 false negative가 기존 CLI보다 1건 넘게 증가하지 않음
- model_requested/model_actual/prompt/sampler/usage 100% 기록

실패하면 자동 등록은 계속 금지하고 frame 수·해상도·모델을 각각 한 변수씩만 바꿔 새 test sheet로 재검증한다.

### 16.3 3일 shadow

현재 카메라 1대에서 3개 날짜를 실행한다.

- 최대 48개 VLM job
- owner가 네 슬롯의 선택 이유와 영상 확인
- 매일 무선택 일반 pool 4개를 추가로 사람 blind 확인
- 고객 후보의 시청 가치, 미세행동 후보 회수, 다양성 bucket 분포, audit false exclusion 기록
- 월 비용 projection 계산

통과 조건:

- 중요 행동·고객 가치 clip을 무선택 표본에서 1건이라도 발견하면 selector 자동 승격 금지 및 원인 보정
- `exclude_absent/static` audit에서 false exclusion이 나오면 Gate v3 hardcase로 기록하고 hard skip 금지 유지
- projected monthly cost `<= $10`; 초과하면 분석량 확대가 아니라 Batch/입력 최적화를 먼저 검토
- terminal infra error 0, temp mp4 잔존 0

## 17. Gate v3와의 관계

이 라우터는 Gate v3를 대체하지 않는다.

- Gate evidence는 후보 selection feature다.
- audit에서 발견한 Gate false negative/false static은 v3 hardcase다.
- `product exclude`와 `presence GT`는 분리한다.
- 후보를 못 고른 이유도 저장해 향후 v3 개선 전후 선택 분포를 비교한다.
- v3가 독립 holdout을 통과하기 전에도 네 슬롯과 audit은 유지한다.

## 18. 구현 순서

1. petcam-lab forward migration과 RLS/rollback probe
2. nightly의 순수 episode/slot selector와 offline replay
3. durable job store와 budget ledger
4. direct image Messages API analyzer와 usage/provenance
5. 30개 GT 품질·token 비교
6. launchd를 22·00·02·04시로 변경하되 shadow·자동등록 off
7. 3일 shadow
8. 사용자 검토 후 유지·수정·중단 결정

## 19. 즉시 중단 조건

- 실제 모델 provenance 불일치
- 월 budget guard 우회 또는 비용 원장 누락
- durable job 저장 전 API 호출
- 원본 clip/R2 삭제
- shadow 중 `behavior_logs`/고객 하이라이트 자동 반영
- Gate absent/static을 hard skip으로 사용
- 동일 episode가 반복 선택돼 네 슬롯 다양성이 붕괴

이 중 하나가 발생하면 nightly Claude job만 중지한다. 별도 `activity-v1` worker와 앱 effective activity canary는 건드리지 않는다.
