# 짧은 영상 retention nightly runtime — 구현 보고서 (Task 1~5)

**작성일:** 2026-07-25
**실행 repo:** `/private/tmp/petcam-nightly-short-clip-worker`
**브랜치:** `codex/short-clip-retention-worker` (feature branch commit/push 만 수행)
**최종 판정:** `SHORT_CLIP_RETENTION_NIGHTLY_READY_FOR_DEPLOY_REVIEW`

---

## 0. 시작 계약

validator 전문:

```
HANDOFF_OK task=short-clip-retention-runtime repo=petcam-nightly-short-clip-worker commit=29c73fcc runtime=launchagent@baeg-endeuui-Macmini.local
```

- 시작 시 branch = `codex/short-clip-retention-worker`, HEAD = `29c73fccfd9e4fd27e8180ba58f4ddf30fa34382`(front matter `commit_sha` 정확 일치).
- `uv run pytest -q` baseline = **376 passed**(핸드오프 명시치와 일치).
- 읽은 순서: 이 repo 의 donts(`.claude/donts-audit.md`, `CLAUDE.md` 없음) → design → plan → handoff.
- Lab DB/UI 계약 SOT: `petcam-lab @ 926e5f6`. **이 package 는 소비자만 구현**하고 신규 table 을 실 DB 에서 조회하지 않는다(migration 미적용).

---

## 1. Task별 RED → GREEN & commit

| Task | 내용 | commit |
|---|---|---|
| 1 | 모델(`ShortClipCandidate`/`DetectionResult`/`DeletionClaim` + `round_display_seconds`) · RPC adapter 8종 · config/​.env switch | `e070f4c` |
| 2 | metadata-only 감지 worker · VLM 소비 가드(candidate/store/backfill) | `45514f7` |
| 3 | exact-object R2 삭제 adapter · 삭제 cycle · 내구성 일일 Slack | `9726116` |
| 4 | fail-closed LaunchAgent 설치기 | `ca0279b` |
| 5 | 전체 검증 · 감사 · 이 보고서 | (본 커밋) |

각 Task 는 RED(모듈/함수 부재 → 실패) → 최소 구현 → GREEN 순서를 지켰다:

- **T1**: 모델 테스트 9 RED(ModuleNotFound) → GREEN, store 테스트 13 RED → GREEN(22 passed).
- **T2**: worker 12 RED → GREEN, VLM 가드 2 RED(load_window_candidates·_open_jobs_for_selector 미가드) → GREEN.
- **T3**: r2 3 RED → GREEN, summary 3 RED → GREEN, delete/Slack 10 RED(run_delete_cycle/Slack 부재) → GREEN.
- **T4**: 설치기 4 RED(스크립트 부재) → GREEN(7 passed).

---

## 2. 변경 파일 · 핵심 interface

**신규 (reporter/)**
- `short_clip_retention_models.py` — `round_display_seconds(x)=floor(x+0.5)`(JS Math.round, Python round() 아님), `DeletionClaim.key_fingerprint()`(소문자 sha256 64-hex), repr 은 r2_key/lease_token 비노출.
- `short_clip_retention_store.py` — §4 RPC 8종 wrapper(`list_detection_candidates`·`record_detection`·`claim_media_deletions`·`complete_media_delete`·`fail_media_delete`·`claim/complete/release_retention_notification`) + `load_system_excluded_clip_ids`(bounded chunk 200). RPC 예외는 `ShortClipStoreError`(타입만), false/stale complete·fail 은 `StaleShortClipError`.
- `short_clip_retention_worker.py` — `run(...)`, `run_detection(...)`, `run_delete_cycle(...)`, `maybe_send_slack(...)`.
- `short_clip_retention_summary.py` — `format_short_clip_retention_summary(stats, now_kst)`(§8 한국어 카드, count/안전 라벨만).

**수정**
- `reporter/config.py` · `.env.example` — 감지/쓰기/삭제 독립 switch(기본 전부 비활성) + EXPECTED_HOST/BATCH[1,200]/DELETE[1,30]/report hour.
- `reporter/r2.py` — `delete_clip_object(r2_key)`(exact key 검증 + `delete_object` 1회).
- `reporter/vlm_candidate_indexer.py`·`vlm_store.py`·`vlm_backfill_worker.py` — quarantined/media_deleted 제외 가드(read + filter, 기존 job 불변).
- 신규 `install-launchd-short-clip-retention.sh` + 8개 테스트 파일.

**핵심 계약(핸드오프 정정 반영)**
- `fn_fail_short_clip_media_delete` 는 `(exclusion_id, lease_token, allowlisted_code, now)` 4-인자 — **fingerprint 를 다섯 번째 인자로 넘기지 않는다**. 실패 fingerprint 는 DB 가 code 로부터 파생.
- `complete` 만 `sha256(r2_key).hexdigest()` 소문자 64-hex 를 넘긴다.
- Lab DB 의 활성 lease 재claim·Slack claim 탈취·Owner restore vs 물리삭제 경합 fail-closed 를 존중 — nightly 는 false/stale RPC 결과를 성공으로 보고하지 않는다(`StaleShortClipError` → audit divergence, cycle nonzero).

---

## 3. 전체 검증 결과

| 명령 | 결과 |
|---|---|
| `uv run pytest -q` (baseline 376) | **442 passed**(+66, skip 0) |
| `uv run python -m compileall -q reporter` | OK |
| `bash -n install-launchd-short-clip-retention.sh` | OK |
| `git diff --check` | clean |

pre-existing 실패 0(baseline 376 전부 green 유지). Gate editable 상대경로용 symlink(`/private/tmp/myPythonProjects/gecko-vision-gate`)는 그대로 두고 `pyproject.toml`/lockfile 미수정.

---

## 4. mocked-only R2/Slack 증거

- R2: `delete_clip_object` 테스트는 `get_r2_client` 를 fake 로 monkeypatch — 실제 boto3/네트워크 0. `delete_object` 정확히 1회, `list_objects_v2`/`delete_objects` 호출 시 테스트가 AssertionError.
- Slack: worker 테스트가 `post_slack_fn`/notification RPC 를 주입한 대역으로 검증 — 실제 webhook 전송 0. claim→complete/release·리포트 시각 이전 no-op·활성 claim None 을 대역으로 확인.
- 삭제 cycle/Slack 은 이번 handoff 에서 실제로 실행되지 않는다(switch 기본 비활성 + 주입 대역).

---

## 5. 금지-행위 정적 감사

- **detection 경로 metadata-only**: worker/store/models/summary 에 `download_clip`·`cv2`·`gate_runner`·`load_detector`·`assess_clip`·`sample_frames`·`anthropic`·`claude`·VLM selector 심볼 0(`test_worker_module_has_no_media_or_model_symbols` + grep).
- **exact delete**: `reporter/r2.py` 삭제 경로에 `list_objects`/`delete_objects`/prefix 삭제/`copy_object` 0 — `delete_object` 1회만.
- **기존 결과 불변**: VLM 가드는 `motion_clip_system_exclusions` read + Python filter 만. `29c73fcc..HEAD` 가드 diff 에 `clip_vlm_jobs` update/delete 코드 0(주석만). GT/label/activity/behavior/Python Evidence 결과 테이블 mutation 0. (`vlm_store.py:9` `update_job` 은 pre-existing, 내 diff 밖.)
- **로그·Slack 위생**: store/worker 는 예외 타입만 출력(`ShortClipStoreError`가 raw Supabase 원문 폐기), summary 는 count/안전 라벨만 — raw key/URL/UUID/token/fingerprint/DB·예외 원문 0(테스트로 문자열 부재 검증).
- **추적된 secret/media/creds 0**: `29c73fcc..HEAD` 변경 파일은 reporter/tests/config/installer 뿐 — `.mp4/.jpg/.env/secret/.key/.pem` 추적 0, `.env.example` 은 빈값·숫자 flag 만.

---

## 6. 무변경 확인 (하드 중단 경계)

이번 세션에서 하지 않음:

- Lab migration production apply · main merge · Vercel/앱 배포.
- Mac mini pull/실행 · LaunchAgent bootout/bootstrap(설치기는 temp HOME+stub render 검증만, 실제 설치 0).
- production DB read/write canary · 실제 R2 API/삭제 · 실제 Slack 전송.
- 정책 INSERT · switch 활성화(config/plist 기본 전부 비활성).
- GT/label/activity time/behavior/기존 VLM·Python Evidence 결과 변경.
- 다른 checkout/worktree 파일 수정.
- 허용된 외부 변경: feature branch commit/push 만.

---

## 7. 다음 deployment handoff 에서 검증할 항목

1. Lab migration(`petcam-lab @ 926e5f6`, `2026-07-24_short_clip_device_error_retention.sql`) production apply + rollback probe + advisor critical 0 이후에만 이 worker 를 실 DB 에 붙인다.
2. Phase A shadow: 설치기로 Mac mini(`baeg-endeuui-Macmini.local`)에 설치(enabled=1/write=0/delete=0) → natural StartInterval fire · DB write 0 · R2/VLM 0 · temp 0 · exit 0.
3. Phase A write: `SHORT_CLIP_RETENTION_WRITE_ENABLED=1`, 정책 `enabled=false` 유지 → 전부 `candidate` 기록, quarantine 0.
4. Phase B: P4 Cam 2 정책 enable → 표시 4/11초 40건만 quarantined, 다른 카메라 0, 사람/151 fingerprint 불변.
5. Phase C: 7일 보존 후 delete switch=1, DELETE_LIMIT=30 canary → exact R2 삭제·metadata 잔존·Slack=DB.
6. Slack 일일 카드의 Owner 복구/검수 대기/7일 후 삭제 예정 필드는 현재 per-cycle 근사값 — 배포 시 DB 집계 쿼리로 정밀화할지 결정(§4 8-RPC 외 aggregate 필요).

---

## 8. 최종 판정

**`SHORT_CLIP_RETENTION_NIGHTLY_READY_FOR_DEPLOY_REVIEW`**

Task 1~5 가 TDD RED→GREEN + 전체 442 passed + compileall/bash -n/diff-check + 금지-행위 정적 감사(metadata-only 감지 · exact delete · 기존 결과 불변 · 로그/Slack 위생 · secret/media 미추적)로 검증됐다. 삭제·Slack·설치는 switch 기본 비활성 + mock 으로만 실행됐고, migration/main/Mac mini/LaunchAgent/production DB/R2/Slack 은 무변경이다. 배포는 §7 게이트로 별도 승인 이후 진행한다.
