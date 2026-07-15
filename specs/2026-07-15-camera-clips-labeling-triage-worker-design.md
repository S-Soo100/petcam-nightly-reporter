# camera_clips 라벨링 격리 제안 Worker 설계

> 상태: Preview 30 완료, owner blind 검토 대기
> 작성일: 2026-07-15
> 구현 레포: `petcam-nightly-reporter`

## 0. 현재 상태 (2026-07-15)

- worker·preview CLI·fail-closed LaunchAgent 설치 스크립트 구현 완료
- 전체 테스트 141개 통과
- production DB migration과 원자성 rollback probe 완료 (`petcam-lab`)
- production labeling web 격리함 배포 및 owner E2E 완료
- 2026-06-30~2026-07-08 범위에서 read-only Preview 30 완료
  - `label` 27건
  - `quarantine` 3건 (`gate_absent` 1, `gate_static` 2)
  - `unknown` 11건은 안전하게 `label` 유지
  - 다운로드/Gate/임시 파일 실패 0건
- Preview 전후 triage 상태/event row는 모두 0건으로 DB write가 없었음
- 다음 승인 경계: owner가 30개 영상을 blind 검토한 뒤 5개 write canary 여부 결정

## 1. 목적

라벨링 큐의 실제 입력인 `camera_clips`를 Mac mini에서 저비용으로 검사해, 라벨링 가치가 낮아 보이는 영상만 `quarantine` 후보로 제안한다. 사람 owner가 격리함에서 최종 결정한다.

이 worker는 `motion_clips`용 activity worker와 실행 목적과 원장을 공유하지 않는다.

## 2. 왜 별도 worker가 필요한가

- 기존 activity shadow worker는 `motion_clips`를 읽는다.
- labeling web은 `camera_clips`를 읽는다.
- 라이브 감사에서 두 집합의 ID overlap은 0건이었다.
- 기존 assessment를 ID 조인하거나 추정 매칭하면 provenance와 정확성을 보장할 수 없다.

따라서 `camera_clips.r2_key`의 영상을 직접 다운로드하고 동일한 Gate sensor를 새로 실행한다.

## 3. 책임 경계

### 이 worker가 하는 일

- 처리할 `camera_clips`를 안정적으로 페이지 조회
- R2 영상 임시 다운로드
- 정해진 frame sampler로 프레임 추출
- Gate evidence와 activity policy 계산
- 안전 조건을 만족할 때 triage suggestion RPC 호출
- provenance와 최소 evidence 저장
- 실행 요약과 실패 수 로깅

### 하지 않는 일

- 원본 영상 삭제
- `behavior_labels`, GT, labeling session 쓰기
- Flutter 활동시간 변경
- 기존 `clip_activity_assessments` 복사
- Claude/VLM 호출
- owner 결정 덮어쓰기
- unknown/error 자동 격리

## 4. 입력 후보 계약

기본 후보는 다음 조건을 모두 만족한다.

- `camera_clips.has_motion = true`
- `camera_clips.r2_key is not null`
- `clip_labeling_sessions`가 한 건도 없어야 함
- owner 최종 결정 `label` 또는 `skip`이 없어야 함
- 동일 evidence identity로 이미 처리 완료된 suggestion이면 재처리하지 않음

조회는 cursor pagination으로 수행한다. offset 기반 전체 스캔이나 첫 페이지 반복으로 starvation을 만들지 않는다.

## 5. Evidence identity

한 번 처리한 결과를 재현하고 정책 변경 시 새 판정을 분리하기 위해 identity를 다음으로 고정한다.

- `clip_id`
- Gate model version
- checkpoint SHA-256
- schema version
- threshold/preset version
- sampler version
- frames sampled
- triage policy version

이 값은 `evidence_snapshot.provenance`에 저장한다. identity가 같으면 idempotent reuse하고, 하나라도 다르면 새 제안 evidence로 평가할 수 있다.

## 6. 판정 규칙

| Gate/activity 결과 | 시스템 제안 | 이유 |
|---|---|---|
| `exclude_absent` | `quarantine` | `gate_absent` |
| `exclude_static` | `quarantine` | `gate_static` |
| `active` | `label` | `gate_active`, 라벨링 큐 유지, 동일 identity 재분석 방지 |
| `unknown` | row 미생성 | fail-open |
| 다운로드/추론/파싱 오류 | row 미생성 | fail-open |

중요:

- `absent`와 `static`은 제품 결과상 격리 후보지만 evidence reason은 분리 보존한다.
- Gate confidence가 낮거나 필수 provenance가 없으면 `unknown`으로 취급한다.
- 시스템 제안은 owner 결정이 없는 경우에만 유효하다.
- 세션이 worker 조회 뒤 생기는 race는 DB RPC가 마지막으로 다시 검사해 막는다.

## 7. DB 쓰기 계약

worker는 service-role 전용 `fn_upsert_clip_labeling_triage_suggestion`만 호출한다.

RPC 입력:

- clip ID
- suggested route
- suggestion reason/source
- policy version
- evidence snapshot

RPC 보장:

- 상태 row와 event row 원자 저장
- owner decision 불변
- 기존 labeling session 존재 시 quarantine 거부
- 동일 identity 재실행 idempotent
- 허용되지 않은 enum/evidence 구조 거부

worker가 테이블에 직접 `upsert`하지 않는다.

## 8. 임시 파일과 자원

- 임시 MP4와 프레임은 OS temp 하위 실행별 디렉터리에만 둔다.
- clip별 처리는 `try/finally`로 정리한다.
- `cv2.VideoCapture.release()`를 항상 보장한다.
- 성공/실패/중단 후 임시 MP4 잔존 0건을 검사한다.
- 단일 worker부터 시작하며 Mac mini 실측 전 병렬도를 높이지 않는다.

## 9. 설정

예상 환경변수:

- `LABELING_TRIAGE_ENABLED=false`
- `LABELING_TRIAGE_WRITE_ENABLED=false`
- `LABELING_TRIAGE_POLICY_VERSION=labeling-triage-v1`
- `LABELING_TRIAGE_BATCH_SIZE`
- Gate checkpoint/preset/sampler 관련 기존 명시값

기본값은 모두 fail-closed다. preview는 `ENABLED=true`, `WRITE_ENABLED=false`로 실행한다. launchd plist는 policy version을 명시하고, null/mismatch면 처리와 DB write를 모두 skip한다.

## 10. 실행 모드

### 10.1 Preview 30

- DB와 R2는 read-only
- 후보 30개를 날짜·시간대가 한쪽에 몰리지 않게 선정
- 결과는 시스템 분석용 CSV/JSON/보고서와 제안을 숨긴 `OWNER-REVIEW.md`로 분리 저장
- 각 행: clip8, 촬영 시각, 카메라, 제안, 쉬운 사유, 최소 provenance
- triage DB write 0건
- 원본 GT/behavior write 0건

실행 결과(2026-07-15): 위 계약을 만족했다. 산출물은 로컬
`storage/labeling-triage-preview-20260715/`에 보관하며 Git에는 포함하지 않는다.

owner가 영상을 blind로 확인해 다음을 기록한다.

- 라벨링 필요
- 라벨링 안 함
- 판단 어려움

Blind 검토 중에는 `OWNER-REVIEW.md`만 사용한다. 시스템 제안·사유·identity가
있는 `REPORT.md`, `preview.csv`, `preview.json`은 30개 판정을 마친 뒤에만 연다.

### 10.2 Write canary

Preview 승인 후 서로 다른 결과를 포함한 소수 clip만 RPC로 저장한다.

- 격리함 pending 노출 확인
- 일반 큐 제외 확인
- owner label/skip/reset 확인
- 세션이 생긴 clip race 409 확인
- owner 결정 후 worker 재실행 시 결정 불변 확인

### 10.3 제한 backfill

canary 승인 뒤 날짜 범위를 명시해 실행한다. 전체 기간 무제한 backfill은 별도 승인 없이는 금지한다.

## 11. 관측성과 실패 처리

실행 요약:

- queried
- reused
- assessed
- suggested_quarantine_absent
- suggested_quarantine_static
- kept_label
- unknown
- skipped_existing_session
- skipped_owner_decision
- failed_download
- failed_gate
- temp_files_remaining

clip 로그는 전체 UUID 대신 clip8을 쓴다. R2 URL, 토큰, 로컬 사용자 경로를 로그에 남기지 않는다.

단일 clip 실패는 다음 clip 처리를 막지 않는다. DB/RPC 전역 장애는 배치를 중단하고 비정상 종료해 무성공처럼 보이지 않게 한다.

## 12. 테스트

### 순수/단위 테스트

- absent → quarantine/gate_absent
- static → quarantine/gate_static
- active/unknown/error → main fail-open
- owner decision이 있으면 write 안 함
- session이 있으면 write 안 함
- identity 같으면 reuse
- identity 변경 시 재평가
- pagination starvation 없음
- temp cleanup
- 로그 redaction

### RPC 통합 테스트

- suggestion+event 원자성
- session race 거부
- owner 결정 불변
- 동일 identity idempotency
- DB 오류 시 부분 상태 0

### 회귀 테스트

- 기존 activity worker 테스트 전부 통과
- 기존 Claude VLM worker 동작 무변경
- Gate 테스트 전부 통과

## 13. 배포 중단 조건

다음 중 하나면 write/launchd를 켜지 않는다.

- Preview에서 실제 라벨링 필요 영상을 quarantine으로 제안
- evidence identity 누락
- owner 결정 덮어쓰기 가능
- 세션 보유 clip 격리 가능
- temporary MP4 잔존
- 앱 활동시간 또는 GT 테이블 변화
- policy version mismatch인데 처리 진행

## 14. 구현 및 운영 승인 경계

완료된 승인 단계:

- production DB migration 적용 및 rollback probe
- preview 실제 실행(DB write 0)
- worker 코드 main commit/push

다음은 각각 사용자 명시 승인 후에만 한다.

- triage suggestion 5개 write canary
- write-enabled launchd 설치
- 제한 backfill
