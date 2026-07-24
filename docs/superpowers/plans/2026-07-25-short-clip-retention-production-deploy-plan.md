# Short-clip retention production deployment implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the reviewed Lab and nightly implementations, apply the production DB contract, and
deploy verified shadow then quarantine-only operation on the Mac mini.

**Architecture:** Supabase remains the policy, state, event, and lease SOT. The Mac mini worker first runs
read-only shadow and advances to writes only when counts match. Physical R2 deletion stays disabled.

**Tech Stack:** PostgreSQL/Supabase, Python 3.12/uv/pytest, Next.js/Vercel, macOS launchd, Slack webhook.

## Global constraints

- Read the companion design completely.
- Use only the exact reviewed SHAs in the design.
- Integration is fast-forward only; force, reset, rebase, and commit rewrite are forbidden.
- Do not expose full UUIDs, R2 keys, credentials, tokens, fingerprints, DB messages, or webhook details.
- Do not change GT, labels, consensus, behavior, app activity, existing VLM results, or Python Evidence.
- `SHORT_CLIP_RETENTION_DELETE_ENABLED=0` for the entire handoff.
- Do not make additional source-code fixes. A code/runtime mismatch is a stop-and-report condition.

---

### Task 1: Revalidate and fast-forward both mains

**Inputs:**
- Lab tip: `926e5f6d500992552e9762c97f05af6a29161588`
- Nightly tip: `75819399bbdb87ee84e8525184fb3ea9d48bb817`

- [ ] Fetch both repos and confirm each feature tip exists, is pushed, and is a descendant of its current
  `origin/main`.
- [ ] Use disposable clean worktrees. Run Lab `uv run pytest -q`, `cd web && npm test`,
  `npx tsc --noEmit`, and `git diff --check`.
- [ ] Run nightly `uv run pytest -q`, `uv run python -m compileall -q reporter`,
  `bash -n install-launchd-short-clip-retention.sh`, and `git diff --check`.
- [ ] Fast-forward each `main` to the reviewed tip and push without force. Confirm
  `local main == origin/main` in both repos.
- [ ] If either ancestry check or regression fails, stop before production mutation.

### Task 2: Apply and adversarially verify the migration

**File:**
- Lab `migrations/2026-07-24_short_clip_device_error_retention.sql`

- [ ] Confirm the migration is not already recorded. Apply it once through Supabase migration tooling.
- [ ] In a transaction that is rolled back, prove:
  - `<15` with no policy is candidate, never quarantine;
  - exact camera policy with display-second 4/11 produces shadow `quarantined`;
  - other lengths and other cameras remain candidate;
  - protected attachments produce `protected/deletion_blocked`;
  - append-only events reject update/delete;
  - active delete and Slack claims cannot be stolen;
  - stale complete/fail tokens are rejected;
  - restore is rejected while a delete lease exists;
  - malformed fingerprint and client-role access are rejected.
- [ ] Confirm rollback residue zero, RLS on, client policies zero, explicit service-role grants, and no new
  critical/error advisor finding.
- [ ] Compare fingerprints/counts for pre-existing human and analysis tables before/after.

### Task 3: Configure the exact camera policy

- [ ] Read cameras by exact name `P4 Cam 2(dev)` and require exactly one result.
- [ ] Resolve exactly one approved Owner account for audit fields without printing its UUID.
- [ ] Insert or idempotently update the one policy to the exact values in design §3.
- [ ] Read it back and verify enabled, `{4,11}`, `<15`, 168 hours, and rule version.
- [ ] Run a read-only historical count matrix by camera and displayed second. Record counts only.
- [ ] If 4/11 is not concentrated on the intended camera or protected rows would be quarantined, set
  `enabled=false` and stop.

### Task 4: Deploy Phase A shadow on Mac mini

- [ ] Connect to `baeg-endeuui-Macmini.local`; confirm hostname, clean repos, and no duplicate worker on
  the MacBook.
- [ ] Pull exact Lab/nightly main SHAs from Task 1. Run focused and full nightly tests on Mac mini.
- [ ] Install `com.petcam.short-clip-retention` with enabled=1, write=0, delete=0 and expected host equal
  to the measured hostname. Verify plist lint, working directory, module, interval 3600, and environment.
- [ ] Kickstart once. Verify exit 0, metadata-only logs, count-only output, temp media zero, R2 calls zero,
  Slack secret zero, exclusion/event row delta zero, and existing analysis table fingerprints unchanged.
- [ ] Compare route counts to SQL recomputation for the same candidate range.
- [ ] Wait for one natural hourly cycle and repeat the same checks. Kickstart alone is not verification.
- [ ] On mismatch, disable the agent and policy, preserve evidence, and stop.

### Task 5: Deploy Phase B quarantine-only

- [ ] Reinstall the agent with enabled=1, write=1, delete=0. Verify the rendered plist before bootstrap.
- [ ] Run one bounded canary cycle. Independently recompute every new state:
  - intended camera display 4/11 and unprotected → quarantined;
  - intended camera display 4/11 and protected → deletion_blocked;
  - all other sub-15 candidates → candidate;
  - no other state.
- [ ] Verify consumer guards: no quarantined/media_deleted clip is returned by new labeling, slot, canary,
  Python Evidence, regular VLM, or backfill selection. Existing rows remain unchanged.
- [ ] Verify Owner restore on one reversible canary, then let the next cycle return `reused_restored`
  without re-quarantine.
- [ ] Observe one natural hourly cycle with exit 0 and no audit divergence.
- [ ] Confirm delete claim count zero, R2 delete zero, temp media zero, and delete switch remains 0.

### Task 6: Verify web and operational reporting

- [ ] Confirm Vercel production deploy for the Lab main commit is READY and `label.tera-ai.uk` serves the
  new system-exclusion route.
- [ ] Perform code/API smoke without credentials: unauthorized access is rejected and raw fields are absent.
- [ ] Confirm the daily Slack card uses DB/KST aggregate counts, contains the Mac mini/rule label, and has
  no URL/key/token/UUID/fingerprint. Do not send duplicate manual cards.
- [ ] Verify MacBook has no short-clip LaunchAgent and Mac mini has exactly one loaded instance.

### Task 7: Close SOT and report

- [ ] Add an operational closure block to Lab and nightly `specs/next-session.md` without rewriting history.
- [ ] Write
  `docs/handoff-prompts/2026-07-25-short-clip-retention-production-deploy-report.md` with:
  exact main SHAs, migration name, policy count-only evidence, Phase A/B route/state counts, natural-cycle
  evidence, LaunchAgent state, Vercel state, Slack result, rollback readiness, R2 delete zero, temp zero,
  and all non-actions.
- [ ] Commit/push documentation only after runtime evidence is complete.
- [ ] Claim `SHORT_CLIP_RETENTION_QUARANTINE_DEPLOYED_VERIFIED` only if every acceptance item passes.
  Otherwise report the exact blocked/rolled-back phase.
- [ ] Stop. Do not enable physical deletion; that needs a new handoff after 168 hours.
