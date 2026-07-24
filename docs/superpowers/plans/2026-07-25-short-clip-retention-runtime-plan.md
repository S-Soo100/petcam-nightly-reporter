# Short-clip retention runtime implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Mac mini metadata detector, VLM exclusion guards, exact-object R2 deletion adapter,
durable Slack summary, and fail-closed LaunchAgent installer without deploying or touching production.

**Architecture:** Supabase service-role RPCs remain the state and lease SOT. The worker is metadata-only
until a DB-issued exact delete claim exists. Detection, writes, and deletion have independent switches;
all operational effects remain disabled in this implementation handoff.

**Tech Stack:** Python 3.12, uv, pytest, Supabase Python client, boto3 R2, macOS launchd.

## Global constraints

- Read the companion design completely before changing code.
- TDD: add a focused failing test, confirm RED, implement the minimum, confirm GREEN.
- `uv` only; do not use `pip`.
- Keep existing VLM selector/job rows intact.
- Host guard precedes lock/DB/R2/Slack.
- No raw R2 key, URL, endpoint, database message, exception message, secret, token, UUID, or fingerprint
  in stdout, stderr, Slack, or committed fixtures.
- R2 tests use mocks; actual R2 calls are forbidden.
- Do not apply migration, merge main, deploy, install launchd, or run on Mac mini.
- Commit each task and push the feature branch; do not force push or rewrite commits.

---

### Task 1: Models, store adapter, and switch configuration

**Files:**
- Create: `reporter/short_clip_retention_models.py`
- Create: `reporter/short_clip_retention_store.py`
- Modify: `reporter/config.py`
- Modify: `.env.example`
- Create: `tests/test_short_clip_retention_models.py`
- Create: `tests/test_short_clip_retention_store.py`

**Interfaces:**
- Produce `ShortClipCandidate`, `DetectionResult`, `DeletionClaim`.
- Produce `round_display_seconds(duration_sec: float) -> int`.
- Produce store functions for every RPC in design §4.

- [ ] **Step 1: Add RED model tests**

Test `3.5 -> 4`, `10.5 -> 11`, and rejection of negative, NaN, and infinity. Test strict parsing of
candidate rows, detection routes, delete claims, UUID/token presence, and lowercase SHA-256 fingerprints.

Run:

```bash
uv run pytest -q tests/test_short_clip_retention_models.py
```

Expected: RED because the module does not exist.

- [ ] **Step 2: Implement immutable models and rounding**

Use `math.floor(duration_sec + 0.5)` after finite/nonnegative validation. Do not use Python `round()`.
Dataclasses must expose only the fields required by the worker; their repr must not include raw R2 keys
or lease tokens.

- [ ] **Step 3: Add RED store tests**

Use a fake Supabase client and cover:

- object/list response normalization;
- unknown detection route rejection;
- DB error converted to a stable internal code without raw message;
- list cursor and limits;
- complete false treated as failure;
- fail RPC signature `(exclusion_id, lease_token, allowlisted_code, now)` with no fingerprint argument;
- Slack claim/complete/release signatures.

- [ ] **Step 4: Implement store adapter and config**

Add:

```python
SHORT_CLIP_RETENTION_ENABLED = os.environ.get("SHORT_CLIP_RETENTION_ENABLED", "0") == "1"
SHORT_CLIP_RETENTION_WRITE_ENABLED = (
    os.environ.get("SHORT_CLIP_RETENTION_WRITE_ENABLED", "0") == "1"
)
SHORT_CLIP_RETENTION_DELETE_ENABLED = (
    os.environ.get("SHORT_CLIP_RETENTION_DELETE_ENABLED", "0") == "1"
)
SHORT_CLIP_RETENTION_EXPECTED_HOST = os.environ.get(
    "SHORT_CLIP_RETENTION_EXPECTED_HOST", ""
)
SHORT_CLIP_RETENTION_BATCH_LIMIT = min(
    max(int(os.environ.get("SHORT_CLIP_RETENTION_BATCH_LIMIT", "100")), 1), 200
)
SHORT_CLIP_RETENTION_DELETE_LIMIT = min(
    max(int(os.environ.get("SHORT_CLIP_RETENTION_DELETE_LIMIT", "30")), 1), 30
)
```

Store functions pass timestamps as ISO strings and never log Supabase error text.

- [ ] **Step 5: Verify and commit Task 1**

```bash
uv run pytest -q tests/test_short_clip_retention_models.py \
  tests/test_short_clip_retention_store.py
uv run python -m compileall -q reporter
git diff --check
git add reporter/short_clip_retention_models.py reporter/short_clip_retention_store.py \
  reporter/config.py .env.example tests/test_short_clip_retention_models.py \
  tests/test_short_clip_retention_store.py
git commit -m "feat: 짧은 영상 retention RPC·설정 계약"
```

---

### Task 2: Metadata-only worker and VLM consumer guards

**Files:**
- Create: `reporter/short_clip_retention_worker.py`
- Modify: `reporter/vlm_candidate_indexer.py`
- Modify: `reporter/vlm_store.py`
- Modify: `reporter/vlm_backfill_worker.py`
- Create: `tests/test_short_clip_retention_worker.py`
- Create: `tests/test_short_clip_retention_vlm_guard.py`
- Modify: `tests/test_vlm_backfill_worker.py`

**Interfaces:**
- Produce `run(...) -> int`.
- Produce `load_system_excluded_clip_ids(sb, clip_ids) -> set[str]`.

- [ ] **Step 1: Add RED worker safety tests**

Cover:

- disabled means DB client, lock, R2, Slack calls are zero;
- blank/mismatched expected host returns nonzero before lock/DB;
- lock loser is a clean no-op with DB/R2/Slack zero;
- shadow uses detection RPC with write false;
- write switch uses true;
- deletion switch false means delete claim/R2 zero;
- no download/OpenCV/Gate/detector/model/LLM/VLM symbol is used by detection;
- a malformed row is counted and isolated;
- candidate-list/DB-wide error returns nonzero;
- duplicate response `reused` is success.

- [ ] **Step 2: Implement worker detection loop**

Reuse `reporter.vlm_host_guard.require_expected_host` and a dedicated nonblocking flock path. The order is:

```text
enabled check → host guard → flock → Supabase client → candidate pagination → record RPC
```

The worker passes only clip UUID, timestamp, and write flag to the record RPC. It prints aggregate counts
only.

- [ ] **Step 3: Add RED VLM guard tests**

Cover:

- `quarantined|media_deleted` are hard-excluded before regular window selection;
- due/recovery open jobs for those clips are not returned;
- rolling backfill includes those IDs in its exclusion set;
- `candidate|restored|deletion_blocked` remain eligible;
- existing `clip_vlm_jobs` rows are not updated/deleted;
- chunking handles more than 1000 clip IDs without omission.

- [ ] **Step 4: Implement bounded VLM guards**

Add a bounded chunk query against `motion_clip_system_exclusions`. Apply it to:

- `load_window_candidates`,
- `_open_jobs_for_selector`,
- rolling backfill `exclude_ids`.

No global unbounded `.select("*")` call is allowed.

- [ ] **Step 5: Verify and commit Task 2**

```bash
uv run pytest -q tests/test_short_clip_retention_worker.py \
  tests/test_short_clip_retention_vlm_guard.py \
  tests/test_vlm_backfill_worker.py
uv run python -m compileall -q reporter
git diff --check
git add reporter/short_clip_retention_worker.py reporter/vlm_candidate_indexer.py \
  reporter/vlm_store.py reporter/vlm_backfill_worker.py \
  tests/test_short_clip_retention_worker.py tests/test_short_clip_retention_vlm_guard.py \
  tests/test_vlm_backfill_worker.py
git commit -m "feat: 짧은 영상 metadata worker·VLM 격리 가드"
```

---

### Task 3: Exact-object R2 deletion and Slack audit

**Files:**
- Modify: `reporter/r2.py`
- Modify: `reporter/short_clip_retention_store.py`
- Modify: `reporter/short_clip_retention_worker.py`
- Create: `reporter/short_clip_retention_summary.py`
- Create: `tests/test_short_clip_retention_r2.py`
- Modify: `tests/test_short_clip_retention_worker.py`
- Create: `tests/test_short_clip_retention_summary.py`

**Interfaces:**
- Produce `delete_clip_object(r2_key: str) -> None`.
- Produce `format_short_clip_retention_summary(stats, now_kst) -> str`.

- [ ] **Step 1: Add RED exact-delete adapter tests**

Assert exactly one mocked call:

```python
client.delete_object(
    Bucket=config.R2_BUCKET,
    Key="terra-clips/clips/exact.mp4",
)
```

Reject blank, `..`, leading slash, trailing slash, bare prefix, and keys outside
`terra-clips/clips/`. Assert no list or bulk-delete method is called.

- [ ] **Step 2: Implement exact-object adapter**

Validate the key before obtaining the R2 client. Translate R2 errors into stable allowlisted categories
without preserving raw response, message, endpoint, or key.

- [ ] **Step 3: Add RED delete-cycle tests**

Cover:

- delete disabled means claim/R2 zero;
- claim empty means R2 zero;
- success calls complete once with lowercase SHA-256 key fingerprint;
- R2 failure calls fail once with `r2_delete_failed`, complete zero, and continues;
- complete false/error after R2 success makes the cycle nonzero and emits an audit-divergence count;
- maximum 30 claims;
- stale claim completion is not reported as success;
- raw key/endpoint/exception text absent from output.

- [ ] **Step 4: Implement bounded delete cycle**

Follow design §7 exactly. Never mutate `motion_clips`, never clear `r2_key`, and never retry by listing
or deleting a prefix.

- [ ] **Step 5: Add RED Slack tests and implement formatter**

Test the exact Korean fields from design §8, KST date handling, one durable daily claim, success complete,
failure release, no-op before report hour, and secret/raw-field absence.

- [ ] **Step 6: Verify and commit Task 3**

```bash
uv run pytest -q tests/test_short_clip_retention_r2.py \
  tests/test_short_clip_retention_store.py \
  tests/test_short_clip_retention_worker.py \
  tests/test_short_clip_retention_summary.py
uv run python -m compileall -q reporter
git diff --check
git add reporter/r2.py reporter/short_clip_retention_store.py \
  reporter/short_clip_retention_worker.py reporter/short_clip_retention_summary.py \
  tests/test_short_clip_retention_r2.py tests/test_short_clip_retention_store.py \
  tests/test_short_clip_retention_worker.py tests/test_short_clip_retention_summary.py
git commit -m "feat: 7일 보존 exact R2 삭제·Slack 감사"
```

---

### Task 4: Fail-closed LaunchAgent installer

**Files:**
- Create: `install-launchd-short-clip-retention.sh`
- Create: `tests/test_install_short_clip_retention.py`

- [ ] **Step 1: Add RED installer tests**

Use a temporary HOME and stub `launchctl`/`plutil`. Assert:

- blank expected host aborts;
- actual hostname mismatch aborts before plist write/bootstrap;
- expected host is never auto-copied;
- plist label/module/working directory/PATH/StartInterval are exact;
- defaults are enabled=1, write=0, delete=0;
- `plutil -lint` occurs before bootstrap;
- output prints all switches;
- log path is `/tmp/short-clip-retention-worker.log`.

- [ ] **Step 2: Implement installer**

Follow existing launchd installers for rendering and bootstrap but do not execute this installer in the
handoff.

- [ ] **Step 3: Verify and commit Task 4**

```bash
bash -n install-launchd-short-clip-retention.sh
uv run pytest -q tests/test_install_short_clip_retention.py
git diff --check
git add install-launchd-short-clip-retention.sh tests/test_install_short_clip_retention.py
git commit -m "feat: 짧은 영상 retention LaunchAgent 설치기"
```

---

### Task 5: Full verification and deployment-review report

**Files:**
- Create: `docs/handoff-prompts/2026-07-25-short-clip-retention-runtime-report.md`
- Modify: `specs/next-session.md`
- Modify: `.claude/donts-audit.md`

- [ ] **Step 1: Run full verification**

```bash
uv run pytest -q
uv run python -m compileall -q reporter
bash -n install-launchd-short-clip-retention.sh
git diff --check
```

Record exact pass/skip counts. If a pre-existing failure exists, prove it against the starting commit;
do not relabel it as success.

- [ ] **Step 2: Run static forbidden-action audit**

Prove:

- detection path has no download/OpenCV/Gate/model/VLM call;
- exact delete contains no list/bulk/prefix operation;
- no GT/label/activity/behavior mutation;
- no secret/raw exception output;
- no production media or generated credentials tracked.

- [ ] **Step 3: Write report**

Include:

- task commits and final SHA;
- changed files and interfaces;
- RED→GREEN tests;
- full verification;
- mocked-only R2/Slack evidence;
- explicit statement that migration/main/Mac mini/LaunchAgent/production DB/R2/Slack were untouched;
- remaining deployment gates.

Maximum verdict:

```text
SHORT_CLIP_RETENTION_NIGHTLY_READY_FOR_DEPLOY_REVIEW
```

- [ ] **Step 4: Commit, push, and stop**

```bash
git add docs/handoff-prompts/2026-07-25-short-clip-retention-runtime-report.md \
  specs/next-session.md .claude/donts-audit.md
git commit -m "docs: 짧은 영상 retention runtime 구현 보고"
git push -u origin codex/short-clip-retention-worker
git status --short --branch
```

Stop. Do not apply the Lab migration, merge main, deploy, run on Mac mini, install LaunchAgent, write
production DB, delete R2 media, or send Slack.
