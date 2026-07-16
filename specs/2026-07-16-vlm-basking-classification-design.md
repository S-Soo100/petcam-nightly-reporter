# VLM 휴식(basking) 분류 복구 설계

> 상태: 사용자 권장안 승인, 구현계획 작성 전 서면 검토
> 작성일: 2026-07-16
> 실행 레포: `/Users/baek/petcam-nightly-reporter`
> 구현 호스트: MacBook
> 런타임 호스트: Mac mini (`com.petcam.vlm-candidate-worker`, `com.petcam.vlm-historical-backfill`)

## 1. 목적

Claude VLM이 게코의 **휴식**을 `moving` 또는 `unseen`으로 강제 분류하는 현재 출력 계약의 공백을 닫는다.

라벨링 정본은 이미 저장 enum `basking`을 화면에서 `휴식`으로 사용한다. 따라서 새 `resting` enum을 만들지 않고 기존 `basking`을 VLM 출력 계약에 복구한다. 쌓인 영상은 Mac mini rolling backfill로 계속 처리하되, 휴식 영상이 일반 이동이나 게코 부재로 오염되지 않게 한다.

## 2. 확인된 문제

현재 Claude VLM의 행동 enum은 다음 7개뿐이다.

```text
eating_paste, eating_prey, drinking, shedding, moving, unseen, hand_feeding
```

반면 사람 blind review 11건은 다음과 같다.

- `unseen`: 3건
- `moving`: 3건
- `basking`으로 정규화할 휴식·머리 살핌: 5건
- 위 5건 중 가림이 커 제품에서 볼 가치가 낮은 영상: 2건

현재 schema로는 마지막 5건에 올바른 답이 존재하지 않는다. 모델의 개별 추론 오류가 아니라 출력 taxonomy의 구조적 결함이다.

## 3. 행동과 제품 판단의 분리

행동 라벨과 제품 노출 가치는 별개 축으로 유지한다.

### 3.1 행동 라벨

- `basking`: 몸의 위치를 옮기지 않고 쉬는 상태. 머리·눈·시선·자세를 조금 움직이며 주변을 살피는 경우도 포함한다.
- `moving`: 몸 전체 또는 몸통의 위치가 영상 안에서 실제로 이동한다.
- `unseen`: 게코를 관찰할 수 없거나 게코 존재를 신뢰성 있게 판단할 수 없다.

경계 규칙:

1. 머리만 조금 흔들거나 방향을 바꿈 → `basking`
2. 같은 자리에서 자세만 조금 바꿈 → `basking`
3. 몸통 위치가 다른 장소로 이동함 → `moving`
4. 게코가 반쯤 가려졌어도 존재와 휴식을 관찰할 수 있음 → 행동은 `basking`
5. 가림이 커 고객에게 보여줄 가치가 낮음 → 행동을 바꾸지 않고 별도 제품 제외 판단으로 처리

### 3.2 제품 노출 가치

이번 변경은 제품 outcome schema를 새로 만들지 않는다. 사람 review에서 명시한 `ab273d21`, `ab8cd4b0`의 제외 판단은 실험 보고서에만 분리 보존한다. `basking` 전체를 제품에서 영구 폐기하지 않는다.

## 4. 코드 계약

### 4.1 Claude CLI batch 경로

`reporter/claude_cli_analyzer.py`의 구조화 출력 enum에 `basking`을 추가한다. 프롬프트에는 3절의 경계 규칙을 쉬운 자연어와 최소 예시로 명시한다.

출력 schema 위반, clip set 불일치, exact model 불일치, host guard, retry/breaker 계약은 변경하지 않는다.

### 4.2 직접 Anthropic 경로의 schema 정합

현재 production provider가 Claude 구독 CLI여도 `reporter/anthropic_analyzer.py`의 schema를 같은 enum으로 유지한다. 직접 API를 활성화하거나 호출하지 않는다.

### 4.3 하이라이트 등록 차단

`basking`은 `moving`, `unseen`, `error`, `shedding`과 마찬가지로 기본 자동 등록 제외 목록에 포함한다. 이번 변경으로 휴식 영상이 새 하이라이트로 자동 등록되면 안 된다.

명시적 환경변수로 기본 목록을 덮어쓰는 기존 동작은 보존한다.

### 4.4 Slack 표시

행동 분포를 표시하는 정규 VLM 요약에서 `basking`을 `휴식`으로 표시한다. 알 수 없는 enum을 `other`로 묶는 기존 안전장치는 유지한다. 현재 rolling backfill Slack은 행동 분포를 표시하지 않으므로 메시지 구조를 확장하지 않는다.

## 5. 데이터 계약

- DB migration 없음: VLM 결과 JSON이 새 enum 문자열을 수용하는 현재 계약을 이용한다.
- 기존 backfill 150건과 기존 성공 결과는 수정·재분류하지 않는다.
- `behavior_labels`, GT, 앱 활동시간, activity filter, Gate 설정을 변경하지 않는다.
- 새 코드 배포 뒤 새로 성공하는 job부터 `basking`이 나타날 수 있다.
- failed/retryable job의 attempt, selector, idempotency 계약은 그대로 유지한다.

## 6. 11건 blind canary

사람 판정은 모델 재시도 결과를 보기 전에 다음 로컬 파일에 고정됐다.

```text
/Users/baek/petcam-lab/storage/retry-review-20260711/human-blind-review.json
```

이 파일과 영상은 git에 커밋하지 않는다. 구현 시 short clip ID와 정규화된 기대 행동만 포함한 비식별 regression manifest를 `petcam-nightly-reporter`의 실험 산출물로 추가할 수 있다. 전체 UUID, 영상 파일, reasoning 원문은 커밋하지 않는다.

canary는 다음 두 단계를 분리한다.

1. **변경 전 기준선**: 11:35 rolling retry의 durable 결과를 읽기 전용으로 보존한다.
2. **변경 후 비교**: 같은 11개 영상을 DB job 상태 변경 없이 진단 전용으로 한 번 분석한다.

변경 후 분석은 production job을 생성·재큐잉·덮어쓰기 하지 않는다. Claude 호출은 사용자 승인 범위인 11개 한정 1회이며, 임시 영상·프레임은 성공/실패와 무관하게 정리한다.

단일 호스트 원칙을 지키기 위해 Claude canary는 MacBook에서 실행하지 않는다. MacBook은 코드·테스트만 담당하고, canary는 Mac mini의 별도 git worktree에서 정확한 feature commit을 checkout한 뒤 shared VLM lock과 host guard를 통과한 경우에만 실행한다. production LaunchAgent의 working directory와 main checkout은 canary 동안 변경하지 않는다.

## 7. 수용 기준

### 7.1 기능

- 구조화 schema가 `basking`을 정상 수용한다.
- `basking`이 자동 하이라이트 등록 대상이 아니다.
- Slack 행동 분포에서 `휴식 N`으로 표시된다.
- 기존 7개 행동의 parse·집계·등록 동작이 회귀하지 않는다.

### 7.2 11건 canary

- 11건 모두 provider 호출이 terminal 결과로 끝나고 infra retryable 0건이다.
- 사람 `unseen` 3건과 `moving` 3건은 각각 정확히 일치한다.
- 사람 `basking` 5건 중 최소 4건이 일치한다.
- 관찰 가능한 `basking` 5건을 `unseen`으로 판정한 사례는 0건이다.
- 불일치가 있으면 원본 영상과 사람 note를 다시 보고 보고서에 원인을 기록한다. 기준 미달이면 Mac mini production 코드를 바꾸지 않는다.

### 7.3 운영 불변

- 정규 VLM selector와 rolling backfill selector 혼합 0건
- succeeded replay 0건
- 중복 job/clip 0건
- host guard, shared lock, 정규 VLM deadline, 일일/cycle 상한 회귀 0건
- 직접 API 비용 0원 유지
- 임시 mp4/frame 잔존 0건

## 8. 배포 순서

1. MacBook의 feature branch에서 TDD 구현과 전체 테스트를 완료하고 commit/push한다.
2. Mac mini의 별도 canary worktree에서 정확한 feature commit을 checkout한다. production main checkout과 LaunchAgent는 건드리지 않는다.
3. MacBook의 로컬 검수 영상 11개를 Mac mini canary 전용 임시 디렉터리에 복사하고, shared VLM lock·host guard를 적용한 진단 전용 canary를 실행한다.
4. 기준 미달이면 feature branch 상태로 중단한다. main과 production runtime은 불변이다.
5. 기준을 충족할 때만 main을 fast-forward하고 push한 뒤, Mac mini production main이 정확한 commit을 pull한다.
6. LaunchAgent plist와 환경변수는 변경하거나 재설치하지 않는다. 서비스가 실행 중이면 종료를 기다린 뒤 pull하고 다음 자연 cycle을 관찰한다.
7. 첫 자연 정규 cycle과 첫 자연 backfill cycle에서 schema 오류 0, selector 분리, Slack `휴식` 표시 가능 상태, 등록 제외, temp 0을 검증한다. 실제 cycle에 `basking` 표본이 없으면 production 실측 전이라고 명시한다.
8. canary용 영상·프레임을 삭제하고 SOT와 next-session에 결과와 잔여 위험을 기록한다.

## 9. 실패와 롤백

- canary가 기준 미달이면 main merge와 production 배포를 중단하고 feature branch에서 사람 판정과 모델 결과를 대조한다.
- production parse에서 `basking` 관련 schema 오류가 나면 두 LaunchAgent를 중단하지 않고 이전 commit으로 fast-forward 가능한 revert commit을 배포한다.
- 이미 저장된 새 `basking` 결과는 삭제하거나 다른 라벨로 덮어쓰지 않는다. 재처리가 필요하면 별도 forward-only 계획을 만든다.
- Slack 실패는 VLM 결과를 재호출하거나 job 상태를 변경하는 이유가 아니다.

## 10. 범위 밖

- Groq VLM 파일럿
- 제품 outcome 신규 DB 필드
- 기존 150건 재분류
- historical failed_terminal 재큐잉
- 라벨링 웹 UI 변경
- Flutter 앱 변경
- Gate v3 재학습 또는 activity filter 변경
