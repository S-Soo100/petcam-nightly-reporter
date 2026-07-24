# Short-clip retention runtime design

## 1. Goal

Mac mini가 `motion_clips` 메타데이터만 읽어 짧은 장치 오류 후보를 DB 정본 RPC에 기록하고,
검증된 카메라 정책에 따라 격리된 영상만 7일 보존 뒤 exact R2 object 단위로 삭제한다.
격리·삭제·Slack은 모두 독립 switch이며 기본값은 쓰기·삭제 비활성이다.

## 2. Source of truth

- Lab DB/UI contract: `petcam-lab` commit
  `926e5f6d500992552e9762c97f05af6a29161588`
- Lab design:
  `docs/superpowers/specs/2026-07-24-short-clip-device-error-retention-design.md`
- Lab full plan:
  `docs/superpowers/plans/2026-07-24-short-clip-device-error-retention.md`
- This runtime implementation starts from nightly `origin/main`
  `6ab6dd2a561c1dee4894b39edad9525341be98cf`.

The Lab migration is not applied to production yet. This package implements and tests the consumer only;
it must not query the new tables in a real run until the deployment handoff applies and verifies the migration.

## 3. Runtime ownership

- Implementation host: `BaekBook-Pro-14-M5.local`
- Runtime host: `baeg-endeuui-Macmini.local`
- LaunchAgent label: `com.petcam.short-clip-retention`
- Module: `reporter.short_clip_retention_worker`
- Schedule: `StartInterval=3600`
- Working directory at runtime: `/Users/baek-end/petcam-nightly-reporter`

`SHORT_CLIP_RETENTION_ENABLED=1`일 때만 worker가 시작된다. 시작 직후
`SHORT_CLIP_RETENTION_EXPECTED_HOST`와 실제 hostname이 정확히 일치해야 하며, 이 검사는 lock,
DB client, R2 client, Slack보다 먼저 실행된다.

## 4. DB RPC contract

The worker consumes only these service-role RPCs.

```text
fn_list_short_clip_detection_candidates(
  double precision, timestamptz, uuid, integer
)

fn_record_short_clip_detection(
  uuid, timestamptz, boolean
)

fn_claim_short_clip_media_deletions(
  integer, text, timestamptz
)

fn_complete_short_clip_media_delete(
  uuid, uuid, text, timestamptz
)

fn_fail_short_clip_media_delete(
  uuid, uuid, text, timestamptz
)

fn_claim_short_clip_retention_notification(
  date, text, timestamptz
)

fn_complete_short_clip_retention_notification(
  date, uuid, timestamptz
)

fn_release_short_clip_retention_notification(
  date, uuid
)
```

Important details:

- Detection passes only clip UUID, current timestamp, and write flag. DB re-derives camera, duration,
  displayed seconds, policy, protection, and state.
- Allowed detection routes are `candidate`, `quarantined`, `protected`, `reused`,
  `reused_restored`, and `ineligible`.
- Delete claims contain only `exclusion_id`, `clip_id`, `r2_key`, and `lease_token`.
- Complete stores `sha256(r2_key).hexdigest()`; it must be lowercase 64-character hex.
- Fail passes only an allowlisted result code. The DB derives the failure fingerprint; the runtime must
  not pass raw exception text.
- A false/stale complete or fail result is not success.
- The DB blocks Owner restore whenever a delete lease exists and prevents active lease re-claim.

## 5. Detection behavior

Candidate discovery is metadata-only. It must not download video, run OpenCV, Gate, detector, local model,
LLM, or VLM.

Switches:

```text
SHORT_CLIP_RETENTION_ENABLED=0
SHORT_CLIP_RETENTION_WRITE_ENABLED=0
SHORT_CLIP_RETENTION_DELETE_ENABLED=0
SHORT_CLIP_RETENTION_EXPECTED_HOST=
SHORT_CLIP_RETENTION_BATCH_LIMIT=100
SHORT_CLIP_RETENTION_DELETE_LIMIT=30
```

- Disabled: DB/R2/Slack calls are all zero.
- Enabled + shadow: candidate list and `fn_record_short_clip_detection(..., false)` only.
- Enabled + write: detection record RPC uses `true`; DB decides whether to quarantine.
- Delete switch false: delete claim and R2 client calls are zero.
- One malformed clip is isolated and counted. A DB-wide failure makes the cycle nonzero.
- Cursor pagination is bounded and stable; batch limit is clamped to 1..200.

## 6. Consumer guards

`quarantined` and `media_deleted` clip IDs must be excluded before new VLM work:

- regular window candidates,
- regular due/recovery jobs,
- rolling historical backfill selection.

The guard must not update or delete existing `clip_vlm_jobs`. States `candidate`, `restored`, and
`deletion_blocked` do not block VLM.

Use bounded chunks when querying `motion_clip_system_exclusions`; never depend on PostgREST's 1000-row
default limit for a global read.

## 7. Exact R2 delete contract

Only `terra-clips/clips/<filename>` keys are accepted. Reject:

- blank key,
- leading or trailing slash,
- `..`,
- a bare prefix,
- a key outside `terra-clips/clips/`.

Call exactly:

```python
client.delete_object(Bucket=config.R2_BUCKET, Key=r2_key)
```

Never list a bucket, delete a prefix, delete multiple objects, or mutate `motion_clips.r2_key`.

For each DB claim:

1. Compute the key SHA-256 fingerprint in memory.
2. Delete exactly the claimed object.
3. Complete with exclusion ID, lease token, fingerprint, and timestamp.
4. On R2 error, call fail once with `r2_delete_failed`; do not log raw exception/key/endpoint.
5. If R2 delete succeeds but complete is false or errors, return nonzero and report audit divergence
   without claiming success.
6. Continue after one R2 failure so one bad object does not starve the rest of the bounded claim set.

R2 delete is never exercised during this implementation handoff; tests use mocks only.

## 8. Slack contract

After the configured KST report hour, claim one durable daily card. On Slack success complete the claim;
on Slack failure release it. An active unexpired claim belongs to one worker only.

```text
🗑️ 짧은 영상 장치 오류
· 후보 34 · 자동 제외 31 · 검수 대기 3
· Owner 복구 0 · 7일 후 삭제 예정 31
· 오늘 R2 삭제 12 · 삭제 차단 1
· 실행 장비: Mac mini · 규칙 short-device-error-v1
```

The message and logs must not include R2 key, URL, UUID, email, lease token, DB message, endpoint,
exception message, or fingerprint.

No-op cycles before the daily report hour send no Slack. `deletion_blocked > 0` or an audit divergence
may send one immediate warning, using a durable or otherwise deterministic dedup key.

## 9. LaunchAgent safety

The installer:

- refuses blank expected host;
- refuses actual hostname mismatch before rendering or bootstrap;
- never copies the current hostname into the expected value;
- renders PATH and all three switches;
- defaults to enabled=1, write=0, delete=0;
- runs `plutil -lint` before bootstrap;
- prints the effective switches;
- uses `/tmp/short-clip-retention-worker.log`.

This handoff implements and tests the installer but does not run it.

## 10. Stop boundary

Allowed:

- feature-branch code, tests, docs, commits, push;
- mocked R2/Slack tests;
- local syntax/unit/regression tests.

Forbidden:

- production migration apply;
- main merge;
- Mac mini pull or process execution;
- LaunchAgent bootout/bootstrap;
- production DB write;
- R2 delete/put/list;
- Slack send;
- policy insert or switch enablement;
- modification of GT, labels, activity time, existing VLM results, or Python Evidence results.

The maximum completion verdict for this handoff is:

```text
SHORT_CLIP_RETENTION_NIGHTLY_READY_FOR_DEPLOY_REVIEW
```
