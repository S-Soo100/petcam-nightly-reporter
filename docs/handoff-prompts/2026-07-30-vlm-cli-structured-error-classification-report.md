# Claude CLI structured error 분류 수정 보고

**상태:** 독립 리뷰·로컬 회귀 검증 완료 / commit·push·runtime 배포 미실행
**작성일:** 2026-07-30
**branch:** `codex/vlm-structured-error-classification-fix`
**base:** `75819399bbdb87ee84e8525184fb3ea9d48bb817`

## 1. 라이브 read-only 확인

- service: `com.petcam.vlm-candidate-worker`
- WorkingDirectory: `/Users/baek-end/petcam-nightly-reporter`
- launchd runs / last exit: `4 / 0`
- schedule: 22:00, 00:00, 02:00, 04:00 KST
- provider/model 설정은 조회만 했고 service·plist를 변경하지 않음
- `/tmp/vlm-candidate-worker.log`는 현재 없어 2026-07-29 17:00 UTC window 4건의
  22:00 이후 개별 상태 전이를 로컬 증거로 복원할 수 없음
- production DB/R2/media/dataset 조회·쓰기와 Claude/provider 호출은 0

따라서 launchd의 마지막 process exit 0은 확인했지만 4건의 회복은 단정하지 않고
`회복 미확정`으로 남긴다.

## 2. 확정 원인

기존 `reporter/claude_cli_analyzer.py::analyze_batch`는 다음 순서였다.

1. `completed.returncode != 0`이면 stdout/stderr marker만 검사
2. marker로 quota/auth를 찾지 못하면 `cli_rc_<n>` retryable
3. returncode 0일 때만 stdout JSON parse와 `is_error/subtype/terminal_reason` 검사

이 때문에 rc=1 + 유효한 `is_error=true`, `subtype=error_max_turns`,
`terminal_reason=max_turns` envelope가 `max_turns_exceeded/no_retry`까지 도달하지 못했다.
plain rc1을 retryable로 두는 기존 정책은 정상이며 변경 대상이 아니다.

## 3. RED -> GREEN

RED fixture:

- returncode `1`
- stdout: 유효한 JSON structured max-turns error envelope
- stderr: 빈 문자열
- 기대: `code=max_turns_exceeded`, `disposition=no_retry`

수정 전 실제 결과:

```text
Actual message: provider_error: cli_rc_1
1 failed
```

최소 수정:

- stdout을 안전하게 JSON parse
- dict + `is_error=true`인 structured error만 generic nonzero rc보다 먼저 분류
- parse 불가 또는 error envelope가 아닌 nonzero rc는 기존 generic rc 경로 유지
- max-turns 상향, subretry 확대, provider 호출 추가 없음

검증:

```text
tests/test_claude_cli_analyzer.py: 26 passed
related VLM worker/audit suite: 104 passed
full reporter suite: 455 passed
```

plain rc1의 `process/retryable/cli_rc_1` 기존 테스트도 통과했다.

독립 read-only Codex 리뷰는 지정한 다섯 분류 경로에서 actionable code finding을 찾지
않았다. structured rc1의 result 기반 auth와 stderr 기반 quota breaker를 직접 고정하는
테스트 공백만 확인해 기존 복합 테스트에 두 assertion을 추가했고 analyzer test 수는
`26`으로 유지했다.

## 4. 반영 경계

- 변경: analyzer 분기 순서, 회귀 테스트, 이 보고서/SOT
- 변경 없음: max turns 3, provider subattempts 2, model, prompt, budget, DB schema,
  R2/media, LaunchAgent/plist
- commit/push/runtime install은 사용자 승인 전이라 실행하지 않음
