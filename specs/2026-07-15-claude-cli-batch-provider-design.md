# Claude Code CLI 배치 Provider 설계

## 목적

Anthropic API 키 없이 `home-mac` Claude Code 구독으로 오늘 밤 VLM shadow 분석을 재가동한다. 후보 선정·DB job 원장·앱 미노출 계약은 기존 budget router v1을 그대로 사용한다.

## 결정

- provider는 `claude_cli_batch`이며 LaunchAgent에서 명시한다.
- 카메라·2시간 창의 후보 최대 4개를 CLI 호출 1회로 묶는다. 각 clip은 6장 JPEG를 사용하므로 호출당 최대 24장이다.
- 모델은 exact ID `claude-sonnet-5`를 요청한다. JSON envelope의 `modelUsage`에 다른 모델만 있으면 모든 결과를 `held_model_mismatch`로 저장하고 breaker를 연다.
- Claude CLI는 `--safe-mode`, `--tools Read`, `--allowed-tools Read`, `--no-session-persistence`, `--output-format json`, `--json-schema`로 실행한다. 임시 프레임 디렉터리 외 접근은 허용하지 않는다.
- 결과는 clip별 job row에 분배한다. provider request id는 CLI session id, token usage는 `modelUsage[exact model]`에서 기록한다.
- 구독 실행의 실제 API 청구액은 `$0`이므로 `cost_usd=0`, `reserved_cost_usd=0`, `pricing_version=claude-code-subscription-v1`로 기록한다. CLI envelope의 환산 cost는 `result.provider_estimated_cost_usd`에 감사용으로만 남긴다.
- 한 batch가 실패하면 그 batch의 job을 동일한 retry 규칙(총 2회)에 따라 함께 갱신한다.
- `direct_api` provider는 기존 코드로 유지하며 API 키가 생기면 설정만 바꿔 전환한다.

## 데이터 흐름

1. 기존 selector가 카메라별 최대 4개 job을 만든다.
2. worker가 due job을 카메라·selector run 단위로 묶는다.
3. R2 다운로드와 6-frame 추출을 clip별로 수행한다.
4. CLI 1회가 clip ID별 구조화 결과 배열을 반환한다.
5. 정확히 요청된 clip ID 집합인지 검증한 뒤 job별 결과·모델·사용량을 저장한다.
6. behavior_logs, camera_clips, 앱 하이라이트에는 쓰지 않는다.

## 실패·중단 조건

- 인증/한도/모델 불일치/구조화 결과 누락은 해당 batch 이후 즉시 breaker stop.
- R2·frame 실패는 그 clip만 retryable/terminal로 처리하고 나머지 clip은 배치한다.
- 후보 0개면 Claude 호출 0회다.
- 오늘 밤 활성화 전 1 clip canary에서 exact model, DB provenance, 임시파일 0, 앱 관련 테이블 write 0을 확인한다.

## 성공 조건

- 전체 테스트 통과.
- Sonnet 5 CLI canary 성공.
- 프로덕션 `clip_vlm_jobs`에 성공 row와 usage가 남는다.
- LaunchAgent가 22/00/02/04 KST에 활성 상태다.
- `com.petcam.activity-worker`는 그대로 유지된다.

