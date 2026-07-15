# Camera Clips Labeling Triage Worker Implementation Plan

> 상태: Task 1~7 및 read-only Preview 30 완료. Owner blind 검토 대기.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mac mini가 라벨링 원본인 `camera_clips`를 Gate로 검사해 `label` 또는 `quarantine` 시스템 제안만 만들고, owner의 결정은 절대 덮지 않게 한다.

**Architecture:** 새 indexer가 `camera_clips`와 session/triage 상태를 batch 단위로 조회한다. 기존 `gate_runner.assess_clip`을 재사용하되 activity 원장에는 쓰지 않고, 전용 policy mapper와 service-role RPC store를 통해 labeling triage에만 기록한다. Preview 30은 동일 파이프라인을 write 없이 실행한다.

**Tech Stack:** Python 3.12, pytest, Supabase, R2/boto3, gecko-vision-gate, OpenCV, launchd.

## Global Constraints

- 설계 정본은 `specs/2026-07-15-camera-clips-labeling-triage-worker-design.md`다.
- 선행 조건은 `petcam-lab`의 `2026-07-15_labeling_triage.sql` 구현이다.
- `camera_clips`를 직접 읽고 기존 `motion_clips` activity assessment를 복사하거나 조인하지 않는다.
- `exclude_absent`와 `exclude_static`만 quarantine 제안이다.
- `active`는 label 제안, `unknown`/오류는 write 없이 일반 큐 유지다.
- labeling session이 하나라도 있거나 owner 결정이 있으면 처리하지 않는다.
- Claude/VLM 호출, GT/behavior/activity/app write, 영상 삭제를 하지 않는다.
- Preview 30은 production DB write 0건이다.
- production DB migration/apply, 실제 preview, launchd 설치, backfill, commit, push는 사용자 명시 승인 전까지 금지한다.

## File Structure

- Create: `reporter/labeling_triage_models.py` — candidate/result dataclasses.
- Create: `reporter/labeling_triage_policy.py` — Gate assessment → suggestion 순수 매핑/evidence snapshot.
- Create: `reporter/labeling_triage_indexer.py` — `camera_clips` cursor scan + session/decision 보호.
- Create: `reporter/labeling_triage_store.py` — suggestion RPC adapter.
- Create: `reporter/labeling_triage_worker.py` — download/Gate/cleanup orchestration.
- Create: `reporter/preview_labeling_triage.py` — 30개 read-only preview CLI.
- Create: matching `tests/test_labeling_triage_*.py` files.
- Modify: `reporter/config.py`, `.env.example` — disabled-by-default settings.
- Create: `install-launchd-labeling-triage.sh` + `tests/test_install_labeling_triage_launchd.py` — installer preparation only.
- Modify: `specs/next-session.md` and operational report docs after implementation.

---

### Task 1: Domain models and pure suggestion policy

**Files:**
- Create: `reporter/labeling_triage_models.py`
- Create: `reporter/labeling_triage_policy.py`
- Create: `tests/test_labeling_triage_policy.py`

**Interfaces:**
- Produces: `LabelingTriageClip`.
- Produces: `TriageSuggestion`.
- Produces: `suggest_from_gate(clip, gate, policy_version) -> TriageSuggestion | None`.

- [x] **Step 1: Write failing policy tests**

Use fabricated `GateAssessment` objects and assert:

```python
exclude_absent -> route='quarantine', reason='gate_absent'
exclude_static -> route='quarantine', reason='gate_static'
active -> route='label', reason preserved only inside evidence
unknown -> None
```

Also assert evidence contains only:

```python
identity, presence, activity, motion, provenance
```

and does not contain checkpoint path, R2 key, producer host, local username, or video bytes.

- [x] **Step 2: Run RED**

Run: `uv run pytest tests/test_labeling_triage_policy.py -q`

Expected: import failure.

- [x] **Step 3: Implement immutable models**

```python
@dataclass(frozen=True, slots=True)
class LabelingTriageClip:
    id: str
    camera_id: str
    started_at: str
    duration_sec: float
    r2_key: str

@dataclass(frozen=True, slots=True)
class TriageSuggestion:
    clip_id: str
    suggested_route: Literal['label', 'quarantine']
    suggestion_reason: Literal['gate_active', 'gate_absent', 'gate_static']
    suggestion_source: str
    policy_version: str
    evidence_snapshot: dict
```

For an active label suggestion, use `suggestion_reason='gate_active'`. The lab migration and shared TypeScript type must accept that exact value.

- [x] **Step 4: Build deterministic evidence identity**

Assign the following dict to `payload`, then canonicalize it with `json.dumps(payload, sort_keys=True, separators=(',', ':'))` and SHA-256 it:

```python
{
  'clip_id': clip.id,
  'model_version': provenance.model_version,
  'checkpoint_sha256': provenance.checkpoint_sha256,
  'schema_version': provenance.schema_version,
  'threshold': provenance.threshold,
  'sampler_version': provenance.sampler_version,
  'frames_sampled': provenance.frames_sampled,
  'triage_policy_version': policy_version,
}
```

Store the full hex digest as `evidence_snapshot['identity']`.

- [x] **Step 5: Run GREEN**

Run: `uv run pytest tests/test_labeling_triage_policy.py -q`

Expected: all policy/provenance tests pass.

---

### Task 2: Starvation-safe camera_clips indexer

**Files:**
- Create: `reporter/labeling_triage_indexer.py`
- Create: `tests/test_labeling_triage_indexer.py`
- Modify: `tests/_fakes.py` only if the fake lacks a query operator used by production code.

**Interfaces:**
- Produces: `list_labeling_triage_candidates(sb, *, start, end, limit, page_size, identity_for_clip) -> list[LabelingTriageClip]`.

- [x] **Step 1: Write failing pagination and protection tests**

Test these exact cases:

```text
first 500 rows already have same evidence identity -> continues to page 2
any clip_labeling_sessions row -> excluded
owner_decision label -> excluded
owner_decision skip -> excluded
same evidence identity -> excluded
different identity -> included for reassessment
missing r2_key or has_motion=false -> excluded
empty page -> normal completion
```

- [x] **Step 2: Run RED**

Run: `uv run pytest tests/test_labeling_triage_indexer.py -q`

Expected: import failure.

- [x] **Step 3: Implement batch-local queries**

For each cursor page select only:

```python
CAMERA_CLIP_FIELDS = 'id,camera_id,started_at,duration_sec,r2_key'
```

Base filters:

```python
.eq('has_motion', True)
.not_.is_('r2_key', 'null')  # use the actual supabase-py spelling verified in this repo
.gte('started_at', start.isoformat())
.lt('started_at', end.isoformat())
.order('started_at')
.order('id')
```

For each page query only its IDs from `clip_labeling_sessions` and `clip_labeling_triage`. Never load all completed IDs. Treat any query error as a raised batch error; do not silently return zero candidates.

- [x] **Step 4: Implement identity comparison**

At worker startup, build an immutable identity config containing model version, checkpoint SHA-256, schema version, threshold, sampler version, frames sampled, and triage policy version. Inject `identity_for_clip(clip_id)` into the indexer. A row is done only when:

```python
row['evidence_snapshot'].get('identity') == expected_identity
```

and owner decision is null. Different identity remains eligible. Any owner decision makes the clip ineligible regardless of identity.

- [x] **Step 5: Run GREEN**

Run: `uv run pytest tests/test_labeling_triage_indexer.py -q`

Expected: pagination and state protection tests pass.

---

### Task 3: Service-role-only suggestion store

**Files:**
- Create: `reporter/labeling_triage_store.py`
- Create: `tests/test_labeling_triage_store.py`

**Interfaces:**
- Produces: `store_triage_suggestion(sb, suggestion) -> StoreResult`.

- [x] **Step 1: Write failing RPC mapping tests**

Assert exact payload keys:

```python
{
  'p_clip_id', 'p_suggested_route', 'p_suggestion_reason',
  'p_suggestion_source', 'p_policy_version', 'p_evidence_snapshot'
}
```

Map results:

```text
ok=true,changed=true -> stored
ok=true,changed=false -> reused
ok=false,labeling_started -> protected_session
RPC/network error -> raise, not fake success
```

- [x] **Step 2: Run RED**

Run: `uv run pytest tests/test_labeling_triage_store.py -q`

Expected: import failure.

- [x] **Step 3: Implement minimal RPC adapter**

The store must call only `fn_upsert_clip_labeling_triage_suggestion`. It must not call a direct table `upsert`. Return an enum-like frozen result so worker stats cannot confuse protected clips with failures.

- [x] **Step 4: Run GREEN**

Run: `uv run pytest tests/test_labeling_triage_store.py -q`

Expected: all store tests pass.

---

### Task 4: Worker orchestration, cleanup, and fail-open behavior

**Files:**
- Create: `reporter/labeling_triage_worker.py`
- Create: `tests/test_labeling_triage_worker.py`
- Modify: `reporter/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `process_triage_batch(sb, clips, detector, policy, policy_version, *, write_enabled, download_fn, assess_fn, store_fn) -> dict[str, int]`.
- Produces: `run(*, sb=None, now=None, enabled=None, write_enabled=None, load_detector_fn=load_detector, download_fn=r2.download_clip, assess_fn=assess_clip, store_fn=store_triage_suggestion) -> int`.

- [x] **Step 1: Add disabled-by-default config**

```python
LABELING_TRIAGE_ENABLED = os.environ.get('LABELING_TRIAGE_ENABLED', '0') == '1'
LABELING_TRIAGE_WRITE_ENABLED = os.environ.get('LABELING_TRIAGE_WRITE_ENABLED', '0') == '1'
LABELING_TRIAGE_POLICY_VERSION = os.environ.get(
    'LABELING_TRIAGE_POLICY_VERSION', 'labeling-triage-v1'
)
LABELING_TRIAGE_WINDOW_HOURS = float(os.environ.get('LABELING_TRIAGE_WINDOW_HOURS', '168'))
LABELING_TRIAGE_BATCH_LIMIT = int(os.environ.get('LABELING_TRIAGE_BATCH_LIMIT', '30'))
```

`.env.example` documents that both booleans default false.

- [x] **Step 2: Write failing worker tests**

Cover:

```text
disabled -> DB/detector/download 0 calls
preview(write=false) -> assess succeeds but RPC 0 calls
write=true active/absent/static -> RPC called with matching suggestion
unknown -> RPC 0 calls
download/Gate failure -> next clip continues and RPC 0 for failed clip
RPC global failure -> nonzero run, no fake success
detector loads once per run
TemporaryDirectory removed after success/failure
```

- [x] **Step 3: Run RED**

Run: `uv run pytest tests/test_labeling_triage_worker.py -q`

Expected: import failure.

- [x] **Step 4: Implement sequential batch processing**

Use existing helpers:

```python
detector = load_detector(config.GATE_CHECKPOINT_PATH, config.GATE_THRESHOLD)
gate = assess_clip(
    mp4_path,
    detector,
    policy,
    config.GATE_CHECKPOINT_PATH,
    num_frames=12,
    clip_id=clip.id,
)
```

Use a worker-specific lock path `/tmp/petcam-labeling-triage-worker.lock`. Download to a per-run `TemporaryDirectory`; each clip uses `Path(tmp)/f'{clip.id}.mp4'`. Do not add manual `sleep` or parallelism.

- [x] **Step 5: Implement exact stats**

Return and log:

```python
queried, assessed, stored_label, stored_quarantine_absent,
stored_quarantine_static, reused, unknown, protected_session,
failed_download, failed_gate, failed_store, temp_files_remaining
```

Use clip8 only in logs. Never print R2 keys, signed URLs, checkpoint paths, or full UUIDs.

- [x] **Step 6: Run GREEN and activity regressions**

Run:

```bash
uv run pytest tests/test_labeling_triage_worker.py -q
uv run pytest tests/test_activity_worker.py tests/test_activity_indexer.py tests/test_activity_store.py -q
```

Expected: new and existing Gate/activity tests pass.

---

### Task 5: Read-only Preview 30 command and review artifact

**Files:**
- Create: `reporter/preview_labeling_triage.py`
- Create: `tests/test_preview_labeling_triage.py`

**Interfaces:**
- CLI: `uv run python -m reporter.preview_labeling_triage --start ISO --end ISO --limit 30 --output DIR`.

- [x] **Step 1: Write failing selection tests**

`select_preview_candidates` must round-robin deterministic strata:

```python
(camera_id, started_at.date(), started_at.hour // 6)
```

It returns at most 30, does not duplicate clip IDs, and does not select all clips from one hour when other strata exist.

- [x] **Step 2: Write artifact safety tests**

The command creates:

```text
preview.json
preview.csv
REPORT.md
OWNER-REVIEW.md
```

Artifacts contain clip8, captured_at, camera_id, suggested route/reason, display reason, evidence identity, and relative local review filename only. They must not contain Supabase keys, R2 credentials, signed URLs, full local home path, or owner email.
`OWNER-REVIEW.md`는 이 중 시스템 제안·사유·evidence를 제외하고 상대 영상 링크와 owner 판정 칸만 제공한다.

- [x] **Step 3: Run RED**

Run: `uv run pytest tests/test_preview_labeling_triage.py -q`

Expected: import failure.

- [x] **Step 4: Implement preview with hard write guard**

Preview calls the same assessment path with `write_enabled=False`. Add a store function that raises `AssertionError` if called, and inject it during preview tests. Exit nonzero if assessed count differs from selected count because silent partial previews are unsafe.

- [x] **Step 5: Run GREEN**

Run: `uv run pytest tests/test_preview_labeling_triage.py -q`

Expected: selection, artifact, and zero-write tests pass.

---

### Task 6: Prepare but do not install a fail-closed LaunchAgent

**Files:**
- Create: `install-launchd-labeling-triage.sh`
- Create: `tests/test_install_labeling_triage_launchd.py`

- [x] **Step 1: Write launcher RED tests**

Using temporary HOME and stubbed `launchctl`, assert generated plist contains:

```text
Label=com.petcam.labeling-triage-worker
ProgramArguments=uv run python -m reporter.labeling_triage_worker
LABELING_TRIAGE_ENABLED=1
LABELING_TRIAGE_WRITE_ENABLED=0
LABELING_TRIAGE_POLICY_VERSION=labeling-triage-v1
PATH including uv bin
StartInterval=3600
```

Also assert `plutil -lint` runs before bootstrap.

- [x] **Step 2: Implement installer**

The committed default must remain `WRITE_ENABLED=0`. The script prints an explicit message that it only accumulates preview/evidence and performs no triage DB write. A later canary approval must modify environment separately; do not embed an automatic enable flag.

- [x] **Step 3: Verify without installation**

Run:

```bash
bash -n install-launchd-labeling-triage.sh
uv run pytest tests/test_install_labeling_triage_launchd.py -q
```

Expected: shell syntax and temporary plist tests pass. Do not run the installer against the real HOME.

---

### Task 7: Full verification and handoff checkpoint

**Files:**
- Modify: `specs/next-session.md`
- Modify: relevant activity/triage report document

- [x] **Step 1: Update status truthfully**

최종 기록:

```text
worker code ready / DB migration applied and probed / preview 30 complete /
write disabled / launchd not installed / triage rows unchanged
```

- [x] **Step 2: Run full verification**

Run:

```bash
cd /Users/baek/petcam-nightly-reporter
uv run pytest
git diff --check
rg -n "motion_clips|behavior_labels|clip_activity_assessments" reporter/labeling_triage_*.py
```

Expected: all tests pass; any `motion_clips` or forbidden write reference in new worker files is absent except explanatory comments/tests.

- [x] **Step 3: Report and stop**

변경 파일, 테스트, 기본 환경, Preview 30 결과를 보고했다. 사용자 자동 진행 승인에 따라 migration·preview·commit·push까지 수행했고, launchd 설치와 triage write는 하지 않았다.

## Post-implementation approval sequence

1. [x] Apply the lab migration and rollback probes.
2. [x] Execute Preview 30 with write disabled.
3. [x] Owner blind review of all 30.
4. [ ] Approve a 5-clip suggestion write canary — **reject**: 3 quarantine 중 false exclusion 2.
5. [x] Verify web queue/quarantine E2E.
6. [ ] Approve a date-bounded backfill — policy v2 독립 holdout 전까지 금지.
7. [ ] Only after evidence accumulation, decide whether to install a write-enabled LaunchAgent.
