# petcam-nightly-reporter 아키텍처 설계

> **상위 SOT**: `tera-ai-product-master/docs/specs/petcam-ai-pipeline.md §11` (Reporting 레이어).
> **기획 초안**: `PLAN.md`.
> 본 문서 = 초안 위에 올리는 **확정 아키텍처** (2026-06-17). PLAN에서 ① Codex→Claude Code, ② 06시 1회→야간 분할, ③ Gate prelabel 소비를 수정 반영.

---

## 1. 포지셔닝 — 공장 (R&D 아님)

`petcam-nightly-reporter` = 연구소(petcam-lab)가 검증한 레시피로 **매일 밤 실제 리포트를 생산하는 공장**. 독립 풀파이프.

- **독립**: petcam-lab의 `behavior_logs`/Claude Inference에 종속되지 않는다. raw clip부터 자체 파이프라인으로 처리.
- **단 레시피는 소비**: lab이 검증한 적응형 프레임 추출·프롬프트·evidence 룰을 가져다 쓴다 (§6). "독립"은 lab에 종속 안 한단 뜻이지 공용 자산까지 버린단 게 아니다.
- **정확도 실험 안 함** — 운영만. 정확도 문제 발견 시 lab에 피드백, lab이 레시피 갱신해 내려보냄 (SOT §11.2 단방향).

---

## 2. PLAN에서 바뀐 것 (PLAN.md 대비) — ⚠️ 재논의 금지

| | PLAN 원안 | **확정 (2026-06-17)** |
|---|---|---|
| 엔진 | Codex CLI (MVP-0) | **Claude Code CLI(구독)** — lab이 Claude 피벗(SOT §11.6), petcam-rba-worker Claude CLI 전례 |
| 실행 주기 | 06시 1회 (§14.1) | **야간 분할 3~4회 + 06 종합** (§4) |
| 입력 | raw clip 자체 처리 | 자체 처리 + **Gate prelabel 재활용** (§7) |
| 범위 | MVP-0/MVP-1 | **MVP-0 집중** (현재 스크립트로 돌아가는 수준). 서버 연결·클라우드 자동화는 나중 |

---

## 3. 실행 환경

- **home mac-mini 24h 가동.** petcam-rba-worker와 같은 머신 공존 → 멀티머신 룰(한 시점 한 자동화 세션은 코드작업 한정, 운영 worker 프로세스는 별개) 적용.
- **트리거**: `launchd` (MVP-0). 야간 분할 시각마다 배치 kick.
- **엔진**: Claude Code CLI(구독). PLAN의 `codex exec` → `claude` 호출로 치환.
- **당장 서버 연결 X**: R2에서 raw clip 읽고 로컬에서 처리 → 로컬/R2 `reports/`에 결과. "설계 완벽 + 현재 스크립트로 돌아가는 PoC"가 이번 목표.

---

## 4. 야간 분할 배치 (핵심 설계)

**8시간(22~06)을 한 방에 X.** 게코 야행성이라 이 구간에 motion clip이 제일 많이 쌓이는데, 한 번에 처리하면 (a) Claude 호출 폭증(한도), (b) 06시 지연, (c) 실패 시 전량 손실. → **N개 윈도우로 쪼개 incremental 처리 + 06 종합.**

```
야간 윈도우 22:00~06:00 (8h)  →  N분할 (N=3~4, 설정값, ~2h씩)

  00:00 배치 → [22:00~00:00] clip → event bundle → 분석 → derived/ 부분 저장
  02:00 배치 → [00:00~02:00]  "
  04:00 배치 → [02:00~04:00]  "
  06:00 배치 → [04:00~06:00]  "  +  밤새 부분결과 merge → 아침 리포트 1장
```

- **분할 배치(00·02·04시)**: 직전 윈도우의 clip만 event bundle + 분석 → 부분 산출물(`events.jsonl` + 부분 리포트 조각)을 `artifacts/derived/`에 저장. **가볍고 빠름.**
- **종합 배치(06시)**: 마지막 윈도우 처리 + 밤새 쌓인 부분결과를 merge → 사용자가 보는 **아침 리포트 1장** (`reports/nightly/YYYY-MM-DD.{md,json}`).
- **이점**: 각 배치 8배 가벼움(한도 안전) · 점진 처리(06시 종합은 merge만이라 빠름) · 실패 격리(한 윈도우 실패해도 나머지 OK) · worker 공존 시 **순간 부하 분산**.
- PLAN §19 미결정("카메라별 chunk vs 1회 호출")을 **시간 기반 chunk**로 답한 것. 카메라가 여러 대면 윈도우 안에서 카메라별 sub-chunk도 가능.
- **멱등**: 윈도우별 처리완료 마킹(로컬 SQLite or derived manifest). 재실행 시 처리된 윈도우 skip.

---

## 5. 데이터 흐름 (PLAN §7 권장 설계 계승)

```
R2 raw mp4 (해당 윈도우)
  → 저해상 motion scan (1~2fps, absdiff)        ← 이벤트 탐지(싸게)
  → event boundary (segment)
  → event당 10~20 개별 프레임 (적응형, lab 레시피 §6)
  → (선택) ROI crop / motion heatmap / metadata
  → Claude Code CLI 분석 프롬프트
  → 부분 리포트 → (06시) 종합
```
- contact sheet는 사람 검토용 보조이지 주 입력 아님 (PLAN §7). 의미 분석 입력 = 개별 프레임.
- 원본 mp4는 재검수/증거 보관용으로 R2에 남김.

---

## 6. lab 레시피 소비 (검증된 자산 재사용)

| 레시피 | 출처(petcam-lab) | 용도 |
|---|---|---|
| 적응형 프레임 추출 | `scripts/_extract_frames_clip.py` (간격3.5s/구간중앙/clamp6~20/no-upscale@1080) | event당 프레임 샘플링 |
| 행동 프롬프트 | `web/prompts/backups/*.v4.0.md` (7-class) | Claude 분석 프롬프트 |
| evidence 룰 | `feature-rba-evidence-based-feeding-drinking.md` (§4 증거 레벨) | 단정 대신 candidate/inferred/confirmed |

> 레시피는 **복사가 아니라 버전 참조**. lab이 v4.1 내면 nightly가 그 버전을 가져온다. drift 방지 = 어느 lab 버전을 쓰는지 리포트 메타에 기록.

---

## 7. Gate prelabel 재활용 (선택, 효율 보너스)

Gate가 `clip_prelabels`를 모든 클립에 깔아두면(SOT §11.3) nightly가:
- **게코 활동 클립만 추리기**: `gecko_visible=true`인 클립만 분석 → 빈 클립 Claude 호출 절약.
- **디코딩 중복 제거**: Gate의 `best_frame_ts`/`bbox`를 시작점으로 → 같은 영상 두 번 풀스캔 안 함.
- **느슨하게**: prelabel 있으면 힌트, 없으면 그냥 자체 motion scan으로 진행 (Gate 의존 강제 X).

---

## 8. worker 공존 — Gate랑 안 겹치게 (SOT §11.4)

| | nightly-reporter | gecko-vision-gate |
|---|---|---|
| Supabase | 읽기만 (camera_clips, clip_prelabels) | `clip_prelabels` 쓰기 |
| R2 | raw 읽기 + `reports/` prefix 쓰기 | raw 읽기만 |
| 로컬 상태 | SQLite (윈도우 처리 마킹) | 0 |

쓰기 영역 교집합 0 → 충돌 차단. 야간 분할이라 Gate와 겹치는 횟수는 늘지만 각 배치가 가벼워 **peak 부하 분산**(분할이 공존에 유리). flock으로 배치 중복 실행 방지.

---

## 9. Claude Code CLI batch (Codex 대체)

```bash
# 분할 배치 (윈도우 처리)
uv run python -m reporter.build_bundle --window 2026-06-17T22:00 2026-06-18T00:00
claude -p "artifacts/derived/.../manifest.json 읽고 이 윈도우 event 분석해서 부분 리포트 써"

# 06시 종합
uv run python -m reporter.merge_night --date 2026-06-17
claude -p "밤새 부분 리포트 merge해서 reports/nightly/2026-06-17.md 아침 리포트 작성"
```
- petcam-rba-worker가 Claude CLI batch를 SegmentVLM에 쓴 전례 → 호출 패턴 재사용.
- 무인 실행 인증: 구독 CLI라 OAuth. MVP-0(mac-mini 로컬)은 OK, MVP-1 클라우드 가면 API key 재검토(PLAN §12).

---

## 10. 구현 단계 (MVP-0, PLAN §18.1 기반)

| Step | 내용 | 완료 조건 |
|---|---|---|
| 1 | pyproject + 패키지 골격 + `.env.example` | `uv run` 동작 |
| 2 | R2 indexer (윈도우별 clip 목록) | 특정 시간창 mp4 리스트 |
| 3 | `motion_scan` + `event_segmenter` | event boundary 추출 |
| 4 | `frame_sampler` (lab 적응형 레시피) | event당 10~20 프레임 |
| 5 | `bundle_writer` (manifest/events.jsonl) | 윈도우 bundle 산출 |
| 6 | **야간 분할 배치 + launchd** | 3~4 윈도우 incremental 처리 |
| 7 | **06시 merge + Claude 리포트** | 아침 리포트 1장 생성 |
| 8 | mac-mini에서 1박 mock run + 품질 검증 | 리포트 품질 사용자 확인 |

> Step 6~7이 이번 핵심(분할+종합). Step 8 = home mac-mini 핸드오프 후 Claude Code 구독으로 검증.

---

## 11. 스코프

**In**: 야간 분할 배치 · event bundle · Claude Code 리포트 · lab 레시피 소비 · Gate prelabel 재활용(선택) · mac-mini 로컬 운영.
**Out (당장 X)**: MVP-1 클라우드 자동화(Fly/Cloudflare/webhook/Queue) · Supabase write-back · Flutter 연동 · 실시간 알림(Slack/Telegram) · 자체 detector 학습(gate 영역).

---

## 12. 리스크 (PLAN §17 계승 + 분할 반영)

| 리스크 | 대응 |
|---|---|
| Claude 한도/비용 | 야간 분할로 배치당 부하 ↓ + P0 후보만 20프레임 |
| 미세행동 누락(drinking/prey) | lab 결론대로 비-VLM evidence + needs_review (프레임만으론 시각한계) |
| AI 과단정 | candidate/inferred/confirmed/needs_review 레벨 (lab evidence 룰) |
| 윈도우 경계 이벤트 잘림 | 윈도우에 약간 overlap(±N분) 두고 merge 시 dedupe |
| mac-mini 꺼짐/실패 | 멱등 재실행 + 다음 윈도우가 놓친 것 흡수. MVP-1에서 클라우드 이전 |
| lab 레시피 drift | 리포트 메타에 사용한 lab 버전 기록 |
