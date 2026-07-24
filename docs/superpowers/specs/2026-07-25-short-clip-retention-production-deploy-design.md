# Short-clip retention production deployment design

## 1. Goal

`P4 Cam 2(dev)`에서 발생하는 표시 길이 4초·11초 장치 오류 영상을 자동으로 라벨링/VLM 신규
소비 대상에서 격리한다. 모든 카메라의 15초 미만 영상은 후보로 기록하되 자동 격리는 검증된
카메라·길이 조합에만 적용한다.

## 2. Approved source

- Lab implementation: `codex/short-clip-retention` at
  `926e5f6d500992552e9762c97f05af6a29161588`
- Nightly implementation: `codex/short-clip-retention-worker` at
  `75819399bbdb87ee84e8525184fb3ea9d48bb817`
- Lab migration:
  `migrations/2026-07-24_short_clip_device_error_retention.sql`
- Runtime host: `baeg-endeuui-Macmini.local`
- LaunchAgent: `com.petcam.short-clip-retention`

Both feature tips are descendants of their current `origin/main`, so integration must remain fast-forward
only. A non-fast-forward result is a hard stop.

## 3. Production policy

Resolve the camera by exact name `P4 Cam 2(dev)` and require exactly one row. Do not hardcode or print its
full UUID.

The sole initial policy is:

- `candidate_under_sec = 15`
- `auto_exclude_display_seconds = {4,11}`
- `retention_hours = 168`
- `rule_version = short-device-error-v1`
- `enabled = true`

`floor(duration_sec + 0.5)` is the display-second contract. `<15` alone never causes automatic quarantine.
Other cameras and other sub-15-second lengths remain candidates only.

`created_by` and `updated_by` must resolve to exactly one approved Owner account on the server. Do not
print the full UUID.

## 4. Phased rollout

### Phase A — shadow

Apply the migration and policy, then install on Mac mini with:

```text
SHORT_CLIP_RETENTION_ENABLED=1
SHORT_CLIP_RETENTION_WRITE_ENABLED=0
SHORT_CLIP_RETENTION_DELETE_ENABLED=0
```

Run one explicit canary and observe one natural `StartInterval=3600` cycle. Compare worker route counts to
read-only SQL. Phase A must create no exclusion/event rows and make no R2 delete.

### Phase B — quarantine

Only after Phase A matches, reinstall with:

```text
SHORT_CLIP_RETENTION_ENABLED=1
SHORT_CLIP_RETENTION_WRITE_ENABLED=1
SHORT_CLIP_RETENTION_DELETE_ENABLED=0
```

Run one bounded canary and one natural cycle. Only `P4 Cam 2(dev)` display-second 4/11 clips without human
GT/research attachments may become `quarantined`. Protected clips become `deletion_blocked`; other short
clips become `candidate`.

`quarantined` and `media_deleted` are excluded from new labeling slots, labeling queues, Python Evidence,
regular VLM, and historical VLM. Existing GT, labels, consensus, VLM results, Python Evidence, motion clip
metadata, and R2 objects remain unchanged.

### Phase C — physical deletion

Not part of this handoff. Keep delete disabled for the complete 168-hour Owner restore window. A later
handoff may enable exact-object deletion only after the first due cohort is audited.

## 5. Safety invariants

- Host guard runs before lock, DB, R2, and Slack.
- Detection is metadata-only: no download, OpenCV, Gate, detector, local model, LLM, or VLM.
- R2 list, prefix delete, and bulk delete are forbidden.
- R2 object deletion is zero in this handoff.
- Existing `clip_vlm_jobs` are read/filter only; no update/delete.
- No raw R2 key, URL, UUID, token, fingerprint, DB error, response body, or secret in logs/Slack/reports.
- All migration tables use RLS with no client policy and service-role-only access.
- Owner restore wins before a delete lease exists. Delete remains disabled, so this handoff issues no lease.

## 6. Rollback

Any mismatch immediately reinstalls the LaunchAgent with enabled/write/delete all `0` or boots it out,
depending on whether evidence collection itself is suspect. Set the camera policy `enabled=false`.
Do not delete audit rows or rewrite history. Report the exact phase, count-only evidence, exit code, and
rollback state.

## 7. Acceptance

The maximum verdict is:

```text
SHORT_CLIP_RETENTION_QUARANTINE_DEPLOYED_VERIFIED
```

It requires migration verification, Phase A shadow match, Phase B quarantine match, a natural hourly cycle,
Mac mini host/HEAD/LaunchAgent evidence, Vercel production readiness, temp media zero, R2 delete zero, and
no forbidden mutation. Physical deletion remains explicitly unverified and disabled.
