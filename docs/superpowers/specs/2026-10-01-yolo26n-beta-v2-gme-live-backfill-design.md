# YOLO26n beta v2 GME Live 전환 + M4 소급 분리 설계

> 상태: Owner 방향 승인, written spec 검토 대기
> 결정일: 2026-10-01
> 선행 결과: beta validation/final B1·B2 통과, protected suite lineage 미완으로 최종 `HOLD`

## 1. 목표

새 YOLO26n beta v2 checkpoint를 현재 운영 Mac mini의 GME에 빠르게 적용해 앞으로 들어오는
production 영상을 새 detector identity로 분석한다. 기존 영상 소급 분석은 오늘 운영 전환과 분리하고,
새 Mac mini M4가 `historical` 작업만 처리하도록 구성한다.

두 호스트가 같은 checkpoint·threshold·tracker 계약을 사용하되 작업 source를 런타임과 DB claim
양쪽에서 강제 분리한다.

## 2. 확인된 현재 상태

- 현재 live GME host: `baeg-endeuui-Macmini.local`
- service: `com.petcam.gme-worker`
- working directory: `/Users/baek-end/gme-v2-runtime/petcam-nightly-reporter`
- runtime repo HEAD: `7d22ebd5a0074ebe14bbb38537cfb9911274adb9`
- 현재 model: `v2.6.1-warm-start-s27`
- 현재 checkpoint SHA-256: `313e933b623561027af9368244c16e8960c74d9b66ec1b919c8d3b58c0ff1c96`
- 현재 detector identity: `81688d0a5ae61299e569573bbc7429bbe362ef9f086cd1cf3e699f9c18c5de2c`
- 현재 score threshold: `0.15`
- 현재 algorithm: `gme-motion-v2`
- M4 host: `baek-mini-m4ui-Macmini.local`; 현재 GME service/repo 없음

새 학습 artifact:

- checkpoint SHA-256: `91a275ad1a047e7a0f70a8a9c1212da7d2b21532bb568a33b3401be9e338751d`
- training report SHA-256: `a60100db1316970d231bd4064cccbdd317f678c1d1920c5143dba59c8359d6ab`
- selection freeze SHA-256: `74d2dc854198b8f871005d591dc18bd5273d7de857ebcea11b4d32f60343c75f`
- frozen score threshold: `0.80`
- frozen post-NMS IoU: `0.55`
- beta final: net recovered `+102`, broken `3`, 7/7 camera nonnegative
- 제한: protected suites 6종 lineage가 없어 최종 decision은 `HOLD`

## 3. 선택안

### 채택: 기존 Mac mini live 전환 + M4 historical 전용

- 기존 운영 Mac mini는 `live`와 명시적 `smoke`만 claim한다.
- M4는 `historical`만 claim한다.
- 두 호스트는 같은 model/checkpoint/inference/tracker provenance를 사용한다.
- 모든 결과는 새 detector identity의 append-only `gme_jobs`/`gme_runs`로 저장한다.
- 기존 identity의 job/run/artifact는 수정·삭제하지 않는다.

이 방식은 오늘 live 품질을 가장 빨리 개선하면서 M4 도입을 별도 장애 변수로 만들지 않는다.

### 보류: 오늘 GME 전체를 M4로 이전

모델 교체와 runtime host 이전을 동시에 하면 장애 원인과 성능 차이를 분리하기 어렵다. M4의 live
승격은 historical 처리 안정성, 처리량, 서비스 재시작·복구 증거를 확보한 뒤 별도 cutover로 다룬다.

### 기각: 같은 detector identity에서 checkpoint 파일 덮어쓰기

provenance와 rollback을 깨뜨리고 기존 결과와 새 결과를 구분할 수 없으므로 금지한다.

## 4. Source 분리 계약

현재 `fn_claim_gme_jobs_for_contract`의 `p_include_historical boolean`은 historical 포함 여부만 제어한다.
M4를 historical 전용으로 제한할 수 없으므로 다음 append-only v2 claim 계약을 추가한다.

```text
fn_claim_gme_jobs_for_contract_v2(
  p_limit,
  p_worker_host,
  p_now,
  p_allowed_sources text[],
  p_detector_identity,
  p_algorithm_version,
  p_engine_schema_version
)
```

- 허용 source는 `smoke`, `live`, `historical`의 비어 있지 않은 부분집합만 받는다.
- 예상 밖 source, 중복 source, 빈 배열은 fail-closed한다.
- 기존 lease 복구·attempt 제한·priority 정렬·`FOR UPDATE SKIP LOCKED`는 그대로 유지한다.
- 기존 v1 RPC는 삭제·수정하지 않는다.
- worker config `GME_ALLOWED_SOURCES`를 strict parse하고 claim v2에 그대로 전달한다.
- live host: `GME_ALLOWED_SOURCES=smoke,live`
- M4 host: `GME_ALLOWED_SOURCES=historical`
- run provenance에 worker host와 source를 보존한다.

## 5. 새 detector 계약

- model family/version: `yolo26n / v2.6.1-beta-v2-s27`
- checkpoint SHA-256: `91a275ad1a047e7a0f70a8a9c1212da7d2b21532bb568a33b3401be9e338751d`
- raw inference confidence: `0.001`
- score threshold: `0.80`
- image size: `960`
- model NMS IoU: `0.70`
- post-NMS IoU: `0.55`
- max detections: `50`
- temporal filter: 5중3
- analysis FPS: `10`
- algorithm: 기존 `gme-motion-v2`
- tracker: 현재 운영의 연결 완화 설정을 그대로 유지

detector identity는 checkpoint와 전체 inference contract를 canonical serialization한 뒤 SHA-256으로
새로 계산한다. 계산된 literal identity는 구현 계획·migration·두 호스트의 launchd config에 동일하게
고정하고 실행 시 다시 계산해 일치하지 않으면 모델 load 전에 중단한다.

- detector identity: `8968c0546e4a86486eb56fb1ab89939da9aedabe3306131ffe8f052669ee0964`

## 6. 오늘 live 전환 순서

1. M4의 학습 checkpoint와 manifest hash를 다시 검증한다.
2. 새 모델을 기존 운영 Mac mini의 새 private version directory에 복사한다.
3. 기존 checkpoint와 plist snapshot을 rollback artifact로 보존한다.
4. protected 입력 중 복구 가능한 표본을 새 frozen 설정으로 평가한다.
5. 새 identity로 최근 production 실영상 10건 smoke를 append-only 실행한다.
6. `10/10 complete`, terminal failure 0, provenance 100%, temp residue 0을 확인한다.
7. current-vs-candidate에서 검출 회복, 새 오검, moving/visible/unknown 변화를 camera별로 보고한다.
8. owner가 승인한 긴급 전환 범위에서 치명적 회귀가 없으면 live trigger와 live worker를 새 identity로
   전환한다. protected lineage 미완은 숨기지 않고 `OWNER_OVERRIDE_ACTIVE_SHADOW`로 기록한다.
9. 새 영상만 새 identity로 enqueue되는지 실제 신규 clip 1건 이상으로 검증한다.
10. 24시간 동안 live lag p95, terminal failure, camera별 visible/moving 분포를 감시한다.

## 7. Rollback

다음 중 하나면 새 live enqueue를 중단하고 이전 identity로 되돌린다.

- checkpoint·freeze·detector identity mismatch
- 10건 smoke 중 complete 미달 또는 terminal failure 발생
- 새 모델에서 기존 사람이 확인한 게코 영상의 검출 파손이 확인됨
- live lag p95 `> 900초`
- run provenance 누락 또는 허용 prefix 밖 write
- 서비스 반복 비정상 종료

rollback은 live trigger와 LaunchAgent config만 이전 literal 값으로 복원한다. 새 identity에서 이미 생성된
job/run/artifact는 감사 provenance로 보존하며 삭제하지 않는다.

## 8. M4 historical 소급

오늘 live cutover와 분리해 다음 순서로 준비한다.

1. M4에 exact runtime repo commit과 checkpoint를 설치한다.
2. `GME_ALLOWED_SOURCES=historical`, M4 hostname guard, 별도 service label을 고정한다.
3. production DB/R2 credential은 기존 secret 전달 방식으로만 설치하고 문서·로그에 출력하지 않는다.
4. historical 10건 smoke에서 old/new host 결과 byte-level provenance와 계산 지표를 비교한다.
5. 통과 뒤 newest-first bounded batch로 소급 enqueue/claim한다.
6. unique key `(clip_id, engine_schema_version, algorithm_version, detector_identity)`로 중복 실행을 막는다.
7. live host의 queue/lag에는 손대지 않으며, DB/R2 부하 또는 오류율 상승 시 M4 historical service만
   pause한다.

M4는 historical만 처리하므로 오늘 이후 새 영상의 지연을 만들지 않는다. 향후 M4를 live host로
승격할 때는 별도 host cutover와 old-host shutdown 검증을 수행한다.

## 9. 쓰기 경계

허용:

- 새 detector identity의 `gme_jobs`와 append-only `gme_runs`
- 기존 GME permanent/debug allowlist prefix
- exact live trigger identity 전환
- 두 호스트의 versioned private model directory와 LaunchAgent plist

금지:

- 기존 job/run/artifact 수정·삭제
- 원본 R2 object 수정·삭제·재인코딩
- 사람 bbox/행동 GT 수정
- 자동 skip·격리·삭제·행동명 확정
- Flutter/API의 legacy activity 값 직접 덮어쓰기
- 동일 identity 아래 다른 checkpoint/threshold 사용

## 10. 완료 기준

### Live

- 새 checkpoint/freeze/identity hash 일치 100%
- production smoke 10/10 complete, terminal failure 0
- 신규 production clip이 새 identity로 enqueue·complete
- live host가 `smoke,live` 외 source를 claim하지 않음
- 24시간 live lag p95 `<= 900초`, terminal failure `< 1%`
- rollback snapshot과 실행 절차 검증

### M4 historical

- runtime hostname/service/cwd/repo HEAD/checkpoint hash 검증
- M4가 `historical` 외 source를 claim하지 않음
- historical smoke 10/10 complete
- duplicate job/run 0, temp residue 0
- live queue 처리 주체와 결과에 영향 0

## 11. 보고 상태

- 코드와 migration 테스트만 완료: `IMPLEMENTED_UNVERIFIED`
- smoke 통과, live 미전환: `PREVIEW_READY`
- live cutover와 실제 신규 영상 검증 완료: `DEPLOYED_VERIFIED_OWNER_OVERRIDE`
- M4 historical smoke와 source isolation 완료: `HISTORICAL_BACKFILL_READY`

protected suites가 모두 검증되기 전에는 통계적 production `ADOPT`라고 부르지 않는다. 긴급 live 전환은
owner가 현재 모델의 실사용 누락을 우선해 승인한 가역 active-shadow override로 정확히 기록한다.
