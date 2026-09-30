# YOLO26n beta v2 GME Live 전환 + M4 소급 분리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 새 YOLO26n beta v2 detector를 기존 Mac mini의 live GME에 가역 적용하고, 새 Mac mini M4가 같은 계약으로 historical 영상만 소급 처리하게 만든다.

**Architecture:** 기존 append-only detector identity와 `gme_jobs`/`gme_runs`를 유지하면서 claim RPC v2에 source allowlist를 추가한다. 기존 Mac mini는 `smoke,live`, M4는 `historical`만 claim하며 checkpoint·inference·tracker contract는 동일하게 고정한다. live는 10건 canary와 rollback snapshot 뒤 전환하고 M4 historical은 별도 10건 smoke 뒤 bounded batch로 시작한다.

**Tech Stack:** Python 3.12, PostgreSQL/Supabase RPC, Ultralytics YOLO26n, OpenCV, gecko-vision-gate, uv, pytest, launchd, Cloudflare R2.

**Spec:** `docs/superpowers/specs/2026-10-01-yolo26n-beta-v2-gme-live-backfill-design.md`

## Global Constraints

- checkpoint SHA-256은 `91a275ad1a047e7a0f70a8a9c1212da7d2b21532bb568a33b3401be9e338751d`다.
- training report SHA-256은 `a60100db1316970d231bd4064cccbdd317f678c1d1920c5143dba59c8359d6ab`다.
- selection freeze SHA-256은 `74d2dc854198b8f871005d591dc18bd5273d7de857ebcea11b4d32f60343c75f`다.
- inference는 raw confidence `0.001`, score threshold `0.80`, image size `960`, model NMS `0.70`, post-NMS `0.55`, max detections `50`, temporal 5중3, 10fps다.
- tracker/algorithm은 현재 운영 `gme-motion-v2` 설정을 유지한다.
- 기존 Mac mini는 `smoke,live`, M4는 `historical` 외 source를 claim하지 않는다.
- 기존 detector identity의 job/run/artifact와 원본 R2 object는 수정·삭제하지 않는다.
- 사람 GT, 행동명, 자동 skip·격리·삭제, Flutter/API legacy activity 값은 변경하지 않는다.
- protected suite 미완을 `ADOPT`로 표현하지 않는다. 긴급 live 적용은 `OWNER_OVERRIDE_ACTIVE_SHADOW`다.
- production write는 승인된 GME job/run/artifact, live trigger identity, versioned runtime/model/plist에만 한정한다.
- cross-repo handoff manifest와 `HANDOFF_OK` 없이 다른 repo/runtime 변경을 시작하지 않는다.

## Review Focus

- source allowlist가 빈 값·중복·미지원 source를 fail-closed하고 다른 source job을 절대 claim하지 않는가.
- 새 checkpoint와 threshold가 같은 literal detector identity로 두 호스트와 DB trigger에 고정되는가.
- live cutover 중 기존 identity queued job을 유실하거나 새 identity job을 처리할 worker가 없는 간격이 생기지 않는가.
- 두 호스트 동시 실행에서 lease·`SKIP LOCKED`·unique key가 중복 run을 막는가.
- rollback이 새 provenance를 삭제하지 않고 trigger/plist만 이전 계약으로 복원하는가.

---

### Task 1: Decision Gate와 exact rollout manifest 고정

**Repo:** `petcam-lab`

**Files:**
- Modify: `docs/decision-gate.md`
- Modify: `docs/superpowers/specs/2026-10-01-yolo26n-beta-v2-gme-live-backfill-design.md`

**Interfaces:**
- Consumes: 승인된 설계, beta v2 artifact hashes, 두 runtime hostname.
- Produces: append-only 4-gate 판정과 두 repo/두 host 실행을 고정한 handoff manifest.

- [ ] **Step 1: decision gate에 이번 방향을 append한다.**

G1은 승인된 GME detector 개선 트랙, G2는 실제 low-contrast miss 개선, G3는 10-smoke·24h live·historical smoke 수치, G4는 이 plan과 rollback으로 각각 통과시킨다. final beta `HOLD`와 owner override를 함께 기록한다.

- [ ] **Step 2: 두 execution repo와 runtime host의 handoff 필드를 동결한다.**

반드시 다음 필드를 포함한다.

```text
execution_repo_lab
execution_repo_reporter
plan_path
design_path
lab_commit_sha
reporter_commit_sha
implementation_host
live_runtime_host
historical_runtime_host
live_service_label
historical_service_label
checkpoint_sha256
selection_freeze_sha256
```

- [ ] **Step 3: 문서 self-review를 실행한다.**

두 repo의 plan/design tracked copy와 최종 implementation commit을 모두 만든 뒤 manifest를 생성하고
`HANDOFF_OK`를 실행하는 단계는 Task 5에서 수행한다. verifier는 plan/design과 execution repo가 같은
Git root에 있어야 하므로 reporter repo에도 승인된 spec/plan의 byte-identical tracked copy를 둔다.

### Task 2: Source allowlist claim RPC v2

**Repo:** `petcam-lab`

**Files:**
- Create: `migrations/2026-10-01_gme_source_allowlist_claim_v2.sql`
- Create: `tests/sql/gme_source_allowlist_claim_v2_fixture.sql`
- Create: `tests/test_gme_source_allowlist_claim_v2_migration.py`
- Create: `scripts/run_gme_source_allowlist_claim_v2_probe.py`
- Modify: `docs/DATABASE.md`

**Interfaces:**
- Consumes: existing `gme_jobs` lease/attempt/priority contract.
- Produces: `fn_claim_gme_jobs_for_contract_v2(..., p_allowed_sources text[], ...)`.

- [x] **Step 1: source isolation RED tests를 작성한다.**

다음을 고정한다.

```python
def test_live_allowlist_claims_only_smoke_and_live(): ...
def test_historical_allowlist_claims_only_historical(): ...
def test_empty_duplicate_or_unknown_sources_fail_closed(): ...
def test_parallel_claims_do_not_duplicate_jobs(): ...
def test_v1_function_and_acl_remain_unchanged(): ...
```

- [x] **Step 2: 테스트 실패를 확인한다.**

Run: `uv run pytest -q tests/test_gme_source_allowlist_claim_v2_migration.py`

Expected: migration/probe가 없어 FAIL.

- [x] **Step 3: append-only v2 RPC와 ACL을 구현한다.**

allowlist 정규화는 허용 enum의 비어 있지 않은 중복 없는 배열만 받는다. lease recovery와 claim 모두 exact detector/algorithm/schema 계약과 `source = any(p_allowed_sources)`로 제한한다. `security invoker`, empty `search_path`, service-role only ACL을 유지한다.

- [x] **Step 4: disposable PostgreSQL probe를 구현하고 실행한다.**

Run: `uv run python scripts/run_gme_source_allowlist_claim_v2_probe.py --pg-bin /absolute/postgres/bin`

Expected: `SOURCE_ISOLATION_OK`, `PARALLEL_CLAIM_OK`, `ACL_PRESERVED_OK`, residue 0.

- [x] **Step 5: migration 관련 회귀 테스트를 실행한다.**

Run: `uv run pytest -q tests/test_gme_source_allowlist_claim_v2_migration.py tests/test_gme_slow_motion_v1_migration.py tests/test_gme_v261_live_identity_cutover.py`

Expected: PASS.

### Task 3: Reporter worker source 제한과 beta v2 모델 계약

**Repo:** `petcam-nightly-reporter`

**Files:**
- Modify: `reporter/config.py`
- Modify: `reporter/gme_store.py`
- Modify: `reporter/gme_worker.py`
- Modify: `install-launchd-gme.sh`
- Modify: `tests/test_gme_store.py`
- Modify: `tests/test_gme_worker.py`
- Modify: `tests/test_install_launchd_gme.py`

**Interfaces:**
- Consumes: Task 2 claim RPC v2, checkpoint/freeze/inference literals.
- Produces: strict `GME_ALLOWED_SOURCES` worker와 검증된 beta v2 runtime detector provenance.

- [x] **Step 1: worker/source/model RED tests를 작성한다.**

```python
def test_config_accepts_canonical_source_allowlists_only(): ...
def test_live_worker_passes_smoke_and_live_to_claim_v2(): ...
def test_historical_worker_passes_only_historical_to_claim_v2(): ...
def test_beta_v2_contract_builds_frozen_detector(): ...
def test_mixed_checkpoint_threshold_identity_stops_before_db(): ...
def test_installer_writes_service_label_and_allowed_sources(): ...
```

- [x] **Step 2: 대상 테스트 실패를 확인한다.**

Run: `uv run pytest -q tests/test_gme_store.py tests/test_gme_worker.py tests/test_install_launchd_gme.py`

Expected: source allowlist와 beta v2 model contract 부재로 FAIL.

- [x] **Step 3: strict config와 claim v2 호출을 구현한다.**

`GME_ALLOWED_SOURCES`는 comma-separated canonical 값으로 읽고 tuple로 정규화한다. worker는 v2 RPC만 사용하며 runtime source와 다른 job이 반환되면 media download 전에 terminal 처리하지 말고 전체 claim-contract violation으로 즉시 중단한다.

- [x] **Step 4: beta v2 model tuple을 installer allowlist에 추가한다.**

checkpoint/freeze/score threshold/model version/detector identity가 모두 일치해야 plist를 생성한다. `GME_SERVICE_LABEL`은 `com.petcam.gme-worker` 또는 `com.petcam.gme-historical-worker`만 허용한다.

- [x] **Step 5: reporter 전체 관련 회귀를 실행한다.**

Run: `uv run pytest -q tests/test_gme_store.py tests/test_gme_worker.py tests/test_install_launchd_gme.py tests/test_enqueue_gme_smoke.py tests/test_enqueue_gme_backfill.py tests/test_audit_gme_shadow.py`

Expected: PASS.

### Task 4: 새 detector identity와 live cutover/rollback 고정

**Repo:** `petcam-lab`

**Files:**
- Create: `migrations/2026-10-01_yolo26n_beta_v2_gme_live_cutover.sql`
- Create: `migrations/2026-10-01_yolo26n_beta_v2_gme_live_rollback.sql`
- Create: `tests/sql/yolo26n_beta_v2_gme_live_fixture.sql`
- Create: `tests/test_yolo26n_beta_v2_gme_live_cutover.py`
- Create: `scripts/run_yolo26n_beta_v2_gme_live_cutover_probe.py`
- Modify: `docs/DATABASE.md`
- Modify: Task 1 handoff manifest

**Interfaces:**
- Consumes: Task 3에서 actual detector instance가 계산한 canonical execution identity.
- Produces: 신규 production clip만 새 identity와 `gme-motion-v2`로 enqueue하는 identity-only cutover와 exact rollback.

- [x] **Step 1: M4에서 checkpoint와 detector identity를 재계산한다.**

hostname, repo HEAD, cwd, checkpoint SHA, selection freeze SHA, model provenance와 계산된 64자리 identity를 private manifest에 기록한다. 예상값을 코드에 미리 발명하지 않는다.

- [x] **Step 2: identity-only RED tests를 작성한다.**

기존 job/run 불변, test clip 미enqueue, production 신규 clip만 new identity, ACL 불변, repeated cutover/rollback idempotence, unexpected function drift fail-closed를 검증한다.

- [x] **Step 3: cutover와 rollback migration을 구현한다.**

현재 production trigger definition hash와 old identity occurrence를 preflight하고 exact literal replacement만 허용한다. DELETE와 기존 job UPDATE는 금지한다.

- [x] **Step 4: disposable PostgreSQL probe와 회귀를 실행한다.**

Run: `uv run python scripts/run_yolo26n_beta_v2_gme_live_cutover_probe.py --pg-bin /absolute/postgres/bin`

Run: `uv run pytest -q tests/test_yolo26n_beta_v2_gme_live_cutover.py tests/test_gme_source_allowlist_claim_v2_migration.py tests/test_gme_v261_live_identity_cutover.py`

Expected: identity-only cutover/rollback, ACL, float constraints, source isolation 모두 PASS.

### Task 5: Cross-repo 검증·커밋·handoff

**Repos:** `petcam-lab`, `petcam-nightly-reporter`

**Files:** Task 1~4 변경 전부.

**Interfaces:**
- Consumes: 두 repo의 구현과 tests.
- Produces: clean tracked commits, exact handoff SHAs, `HANDOFF_OK`.

- [ ] **Step 1: repo별 focused tests와 전체 회귀를 실행한다.**

Lab Run: `uv run pytest -q`
Reporter Run: `uv run pytest -q`

Expected: 기존 baseline 이상의 PASS, 새 failure 0.

- [ ] **Step 2: diff와 비밀값 누출을 감사한다.**

Run: `git diff --check`와 secret/path audit. checkpoint bytes와 credentials는 git에 넣지 않는다.

- [ ] **Step 3: repo별 변경을 독립 커밋하고 push한다.**

예상 메시지:

```text
feat: GME 워커 source별 claim 계약 추가
feat: YOLO26n beta v2 GME live 전환 준비
```

- [ ] **Step 4: manifest에 두 40자리 commit SHA를 갱신하고 다시 커밋한다.**

lab live-control manifest 1개와 reporter runtime manifest 2개(live host, M4 historical host)를 만든다.
reporter repo에는 승인된 spec/plan의 byte-identical tracked copy를 포함해 verifier의 same-repo 계약을
충족한다.

- [ ] **Step 5: handoff verifier를 재실행한다.**

Expected: `HANDOFF_OK` 전문 확보 전 runtime write 금지.

### Task 6: 기존 Mac mini 10건 canary와 live cutover

**Runtime host:** `baeg-endeuui-Macmini.local`

**Private paths:**
- New model: versioned directory under `/Users/baek-end/private-rba/`
- Rollback snapshot: versioned private rollout directory

**Interfaces:**
- Consumes: verified reporter commit, lab migrations, new checkpoint/manifest.
- Produces: 10건 candidate smoke와 신규 live 영상의 새 identity run.

- [ ] **Step 1: production read-only preflight를 고정한다.**

hostname, loaded service, cwd, runtime HEAD, clean state, current plist hash, current model/checkpoint/identity, old-identity queue counts, live lag, recent error count를 기록한다.

- [ ] **Step 2: source claim v2 migration만 먼저 적용하고 read-back한다.**

function definition hash, ACL, old RPC 존재, job/run row count 불변을 확인한다.

- [ ] **Step 3: checkpoint와 manifest를 새 versioned private directory에 전송한다.**

전송 전후 SHA-256 exact match를 확인한다. 기존 checkpoint는 덮어쓰지 않는다.

- [ ] **Step 4: 기존 service는 유지한 채 candidate identity smoke 10건을 enqueue하고 manual one-shot worker로 처리한다.**

candidate 실행 환경은 `GME_ALLOWED_SOURCES=smoke,live`지만 trigger는 아직 old identity이므로 candidate는 명시적 smoke만 처리한다. old live worker는 계속 기존 live를 처리한다.

- [ ] **Step 5: canary gate를 채점한다.**

`10/10 complete`, terminal failure 0, lineage/provenance 100%, temp residue 0, 허용 prefix 밖 write 0을 요구한다. current-vs-candidate의 visible/moving/unknown과 camera별 파손을 보고한다. 치명적 회귀면 cutover하지 않는다.

- [ ] **Step 6: rollback snapshot 뒤 live trigger와 LaunchAgent를 새 identity로 전환한다.**

trigger cutover read-back 후 새 plist를 install한다. old identity claimable live job이 0이거나 별도 drain됐는지 확인한다.

- [ ] **Step 7: 실제 신규 production clip으로 end-to-end를 검증한다.**

새 clip이 new identity의 `source=live` job으로 생성되고 기존 Mac mini가 claim·complete하는지 확인한다. historical claim 0, model hash 일치, service exit 정상이어야 한다.

### Task 7: M4 historical 전용 runtime 구축과 bounded 소급 시작

**Runtime host:** `baek-mini-m4ui-Macmini.local`

**Files/Services:**
- Isolated runtime checkout under M4 private/runtime root
- Service label: `com.petcam.gme-historical-worker`
- `GME_ALLOWED_SOURCES=historical`

**Interfaces:**
- Consumes: Task 5 commits, Task 6에서 검증된 checkpoint/identity.
- Produces: historical-only 10건 smoke와 newest-first bounded backfill.

- [ ] **Step 1: M4 exact repo checkout과 dependency/runtime preflight를 설치한다.**

hostname, cwd, repo HEAD, Python/uv, MPS, checkpoint hash를 검증한다. production credential은 기존 secret channel로만 전달하고 출력하지 않는다.

- [ ] **Step 2: historical-only LaunchAgent를 install하되 즉시 대량 queue를 만들지 않는다.**

plist read-back에서 label, expected host, `historical` allowlist, checkpoint, identity, algorithm이 exact 일치해야 한다.

- [ ] **Step 3: historical 10건을 새 identity로 enqueue하고 smoke한다.**

10/10 complete, terminal failure 0, live/smoke claim 0, duplicate 0, temp residue 0을 요구한다.

- [ ] **Step 4: newest-first bounded batch를 시작한다.**

초기 batch는 작게 시작해 처리량, DB/R2 오류율, swap, thermal pressure를 측정한다. live lag 또는 공유 인프라 오류가 증가하면 historical service만 pause한다.

- [ ] **Step 5: `HISTORICAL_BACKFILL_READY` runtime report를 만든다.**

hostname, service state, cwd, repo HEAD, checkpoint/identity, allowed source, smoke hashes, queue counts, 실제 run 증거를 기록한다.

### Task 8: 24시간 감시와 최종 운영 보고

**Files:**
- Create: private rollout report on each runtime host
- Modify: `specs/next-session.md`
- Modify: `docs/decision-gate.md` with append-only execution result

**Interfaces:**
- Consumes: live and historical runtime evidence.
- Produces: `DEPLOYED_VERIFIED_OWNER_OVERRIDE`와 `HISTORICAL_BACKFILL_READY` 또는 rollback/HOLD 보고.

- [ ] **Step 1: 6시간 간격 monitor를 등록한다.**

unchanged healthy state는 조용히 있고 completion/failure/user action만 알린다. live lag, terminal failure, camera별 visible/moving/unknown shift, host/service/HEAD/config drift를 본다.

- [ ] **Step 2: 24시간 지표와 rollback 조건을 판정한다.**

live lag p95 `<=900초`, terminal failure `<1%`, source isolation 위반 0, repeated service crash 0이어야 한다.

- [ ] **Step 3: 최종 문서와 상태를 append-only로 갱신한다.**

protected lineage 미완은 계속 명시하고 statistical `ADOPT`로 승격하지 않는다. rollback이 실행됐으면 원인과 보존된 paths/hashes를 적는다.

## Execution Order and Stop Rules

1. Task 1~5는 production write 없이 완료한다.
2. Task 6 canary 실패 시 live cutover를 실행하지 않는다.
3. Task 6 live cutover가 검증된 뒤에만 Task 7 historical 소급을 시작한다.
4. M4 문제는 기존 Mac mini live를 rollback하거나 중단시키지 않고 historical service만 멈춘다.
5. 같은 smoke/final claim을 덮어쓰거나 재실행하지 않는다. 기존 immutable 결과는 검증해 재사용한다.
