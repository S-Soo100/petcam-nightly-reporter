# VLM 240개 과거 백필 — 20:30 KST 최종 판정·정리 (one-shot, 무인)

너는 Mac mini에서 이 태스크 하나만 수행하는 headless Claude Code 세션이다. 사람이 옆에 없으므로
스스로 판단해야 하지만, 아래 안전 계약을 벗어나는 어떤 행동도 하지 않는다. 이 프롬프트가
유일한 지시문이다 — 이전 대화의 맥락은 없다. 애매하면 항상 "안전 정지"(§3)를 기본값으로 택한다.
"일단 진행해보고 문제 있으면 되돌리자"는 이 태스크에서 금지된 사고방식이다.

## 0. 컨텍스트

- 주 레포: `~/petcam-nightly-reporter` (main 브랜치)
- SOT 레포: `~/petcam-lab` (같은 머신)
- Gate 레포: `~/myPythonProjects/gecko-vision-gate` (read-only 참조만)
- 정본 스펙: `specs/2026-07-15-historical-vlm-backfill-design.md`,
  `specs/2026-07-15-historical-vlm-backfill-plan.md` (petcam-nightly-reporter 안)
- selector_version = `budget-router-backfill-20260707-14-v1`
- 목표: 2026-07-07~07-14 8개 source night × 30개 = 총 240개를 provider=`claude_cli_batch`,
  model=`claude-sonnet-5`로 shadow 분석. `clip_vlm_jobs`/`clip_vlm_selector_runs`에만 기록하고
  앱·GT·활동시간·behavior_labels·camera_clips는 절대 바꾸지 않는다. 실제 API 비용은 $0(구독 CLI).
- 임시 LaunchAgent `com.petcam.vlm-historical-backfill`이 오늘 시간당 최대 8회 실행되며 8개
  source night를 채운다.
- 정규 LaunchAgent `com.petcam.vlm-candidate-worker`(22/00/02/04시)와 `com.petcam.activity-worker`는
  이 태스크와 무관하다 — 어떤 경우에도 절대 건드리지 않는다.
- 경로나 파일 존재를 절대 기억으로 단정하지 말고 매번 `Read`/`ls`/`git`으로 재확인한다. 이 문서에
  적힌 경로가 실제와 다르면(예: 홈 디렉토리명 변경) 실측값을 우선한다.

## 1. 먼저 할 일 — live 상태 재검증 (read-only, 아무것도 쓰지 않는다)

1. `cd ~/petcam-nightly-reporter && git status && git log --oneline -5` — 브랜치·미커밋 변경 확인.
   untracked `.env.bak-20260708-vlmoff`와 `storage/` 아래 사용자 데이터는 절대 건드리지 않는다.
2. `launchctl print gui/$(id -u)/com.petcam.vlm-historical-backfill` — 여전히 설치돼 있는지,
   최근 실행이 있었는지 확인.
3. DB 상태를 read-only로 집계한다 (아래와 동일한 취지의 python을 직접 실행):

   ```bash
   uv run python -c "
   from collections import Counter
   from supabase import create_client
   from reporter import config
   sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
   rows = sb.table('clip_vlm_jobs').select(
       'status,model_requested,model_actual,cost_usd,error_code,rank_features'
   ).eq('selector_version', 'budget-router-backfill-20260707-14-v1').execute().data
   print('total', len(rows))
   print('status', Counter(r['status'] for r in rows))
   print('model_req', Counter(r['model_requested'] for r in rows))
   print('model_act', Counter(r['model_actual'] for r in rows))
   print('cost_sum', sum(float(r['cost_usd'] or 0) for r in rows))
   print('errors', Counter(r['error_code'] for r in rows if r['error_code']))
   by_date = Counter((r.get('rank_features') or {}).get('source_date') for r in rows)
   print('by_date', dict(sorted((k, v) for k, v in by_date.items() if k)))
   "
   ```

4. `clip_prelabels`, `clip_activity_assessments`, `behavior_labels` row count를 다시 세어 각각
   1397, 1397, 249(또는 이 세션 시작 전 실측값)와 같은지 확인한다 — 이 백필이 GT/활동시간을
   건드리지 않았다는 증거다. 다르면 안전 정지 사유로 기록한다.
5. `uv run pytest -q` (petcam-nightly-reporter) — 실패하면 즉시 안전 정지 사유로 기록한다.

## 2. 안전 정지 조건 — 하나라도 해당하면 §3만 하고 §4는 절대 하지 않는다

- 총 job 수가 240이 아니다(240 미만이거나, 있어서는 안 될 초과분이 있다).
- 어떤 job이든 status가 `queued`/`submitted`/`failed_retryable`/`held_model_mismatch`다
  (아직 진행 중이거나 보류 상태).
- `failed_terminal` job이 하나라도 있다 — 완전 성공이 아니므로 멈춘다.
- `model_requested != model_actual`인 job이 하나라도 있다.
- `model_requested`나 `model_actual`이 `claude-sonnet-5`가 아닌 job이 있다.
- `error_code`가 `quota_exceeded`/`not_logged_in`/`clip_set_mismatch`이거나 그 외 어떤
  error_code든 하나라도 있다.
- `cost_usd` 합계가 0이 아니다.
- §1의 pytest가 실패했거나, clip_prelabels/clip_activity_assessments/behavior_labels 카운트가
  이번 세션 시작 전 값과 달라졌다.
- 그 외 이 문서가 가정한 사실과 실측이 다른 경우 전부.

## 3. 안전 정지 시 행동 (§2 중 하나라도 해당하면 여기서 끝낸다)

- **강행하지 않는다.** 삭제·재선발·스위치 변경·수동 재시도를 하지 않는다.
- 임시 LaunchAgent `com.petcam.vlm-historical-backfill`은 그대로 둔다(bootout 금지) — 다음
  시간별 실행이 계속되게 둔다.
- 원인과 다음 조치를 `storage/vlm-backfill-20260707-14/finalizer-status-<UTC타임스탬프>.md`에
  기록한다. 전체 UUID 대신 clip 앞 8자만 쓰고, 이메일/토큰/키는 절대 쓰지 않는다. 내용:
  §1 집계 요약, 어떤 안전 정지 조건에 해당했는지, 권장 다음 조치(예: "3개 source night 남음,
  다음 실행은 약 N시간 후 예상").
- 이 상태 파일 하나만 로컬에 만들어도 되지만, **git add/commit/push는 하지 않는다**
  (사용자 승인 없는 커밋 금지 원칙).
- 정규 워커·다른 어떤 설정도 변경하지 않는다.
- 표준출력에 결과를 한 문단으로 요약해 남긴다(launchd 로그로 보존된다).

## 4. 완료 시 행동 — §2 조건이 전부 거짓일 때만, 즉 240/240 succeeded·model 일치·cost $0일 때만

1. `uv run python scripts/report_vlm_backfill.py --out storage/vlm-backfill-20260707-14`
   - 실행 후 `REPORT.md`, `jobs.json`, `contact-sheet-2026-07-0{7..9}.jpg`·
     `contact-sheet-2026-07-1{0..4}.jpg`(총 8개)가 생성됐는지 확인한다.
   - `REPORT.md`에 총 240, actual_cost $0.00, model_mismatch 0이 적혀 있는지 확인한다.
2. 임시 mp4 잔존 0 확인: `find /tmp -maxdepth 1 -iname "*.mp4"` 결과가 비어야 한다(report
   스크립트는 TemporaryDirectory에서만 mp4를 받는다).
3. app/GT/activity 불변 재확인: `clip_prelabels`/`clip_activity_assessments`/`behavior_labels`
   카운트가 §1에서 잰 값과 정확히 같은지 다시 비교한다. 하나라도 다르면 report를 만들었더라도
   §3처럼 상태를 기록하고 멈춘다(이 경우도 안전 정지로 취급하고 아래 5~7은 하지 않는다).
4. 임시 LaunchAgent를 해제한다:
   ```bash
   launchctl bootout gui/$(id -u)/com.petcam.vlm-historical-backfill
   rm -f ~/Library/LaunchAgents/com.petcam.vlm-historical-backfill.plist
   ```
   정규 `com.petcam.vlm-candidate-worker`, `com.petcam.activity-worker`는 절대 건드리지 않는다.
5. SOT를 갱신한다(사용자의 다른 변경은 보존 — append/체크박스 갱신만, 무관한 섹션 재작성 금지):
   - `~/petcam-nightly-reporter/specs/2026-07-15-historical-vlm-backfill-design.md`와
     `specs/2026-07-15-historical-vlm-backfill-plan.md`의 관련 완료 조건 체크박스를 실측치와
     함께 갱신한다(240/240, cost $0, mismatch 0, temp mp4 0, 완료 시각).
   - `~/petcam-lab/specs/next-session.md`에 이번 백필 완료 사실을 한 단락으로 추가한다(기존
     내용을 지우거나 재작성하지 않는다 — 파일 끝에 추가하거나 관련 섹션에만 최소 추가).
6. 두 레포에서 전체 테스트와 diff 검사를 돌린다:
   ```bash
   cd ~/petcam-nightly-reporter && uv run pytest -q && git diff --check
   cd ~/petcam-lab && uv run pytest -q && git diff --check
   ```
   하나라도 실패하면 **커밋하지 않고** §3과 동일하게 상태를 기록한 뒤 멈춘다.
7. 커밋·push(conventional commit, 레포별 기존 컨벤션 그대로, Co-Authored-By 자동 유지):
   - `petcam-nightly-reporter`: 이번에 변경한 spec 파일과 `storage/vlm-backfill-20260707-14/`
     산출물만 `git add`한 뒤 `git commit -m "docs: VLM 240개 백필 완료 최종 보고"` →
     `git push origin main`.
   - `petcam-lab`: `specs/next-session.md`만 `git add`한 뒤
     `git commit -m "docs: VLM 240개 과거 백필 완료 기록"` → `git push origin main`.
   - 사용자의 기존 미커밋/untracked 변경(예: `.env.bak-20260708-vlmoff`, 관련 없는 `storage/`
     데이터)은 절대 add/commit/삭제하지 않는다. 이번 태스크가 만든 파일만 커밋 대상이다.
8. 표준출력에 최종 요약을 한 문단으로 남긴다: 240/240 succeeded, cost $0, model_mismatch 0,
   두 레포 커밋 해시, 임시 LaunchAgent 해제 여부, 남은 위험(있다면).

## 5. 항상 지킬 것 (§3/§4 어느 경로든 공통)

- 절대 하지 않는다: `git reset --hard`, `push --force`, `branch -D`, `rm -rf`,
  `clip_vlm_jobs`/`clip_vlm_selector_runs` row 삭제, `behavior_labels`/`camera_clips`/
  `exclude_absent`/`exclude_static` 변경, 정규 22/00/02/04시 VLM LaunchAgent나
  `com.petcam.activity-worker` LaunchAgent의 설정 변경.
- 전체 clip UUID·owner ID·이메일·R2/Supabase credential·구독 계정 정보를 로그·파일·커밋
  메시지 어디에도 남기지 않는다. clip을 가리킬 때는 항상 앞 8자(clip8)만 쓴다.
- 이 세션은 1회성이다. LaunchAgent가 스스로 자신을 해제하므로 너는 다음 실행을 예약하거나
  재시도 루프를 만들지 않는다.
- 이 문서에 없는 예외 상황을 만나면 아무것도 강행하지 말고 §3의 형식으로 상황을 기록한 뒤
  멈춘다.
