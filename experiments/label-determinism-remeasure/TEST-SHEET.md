# TEST-SHEET — P1 오탐 재측정 (label-determinism remeasure)

> pre-reg. **실행 전 고정 — 사후 변경 금지** (petcam-lab `.claude/rules/research-testing.md`).
> 발주: petcam-lab P1 (design `docs/superpowers/specs/2026-07-21-nightly-label-determinism-design.md` §6,
> plan Task 3+5). 진단 정본: `docs/superpowers/specs/2026-07-21-label-path-diagnosis.md`.
> 작성: 2026-07-21, owner "자동진행" 위임 (표본·프롬프트·모델·판정 룰 사전 확정분).

## 1. 가설

- **H1 (대립):** 결정론 경로(temperature=0, prompt v4.0, claude-sonnet-5)에서 기존 오탐 42건의
  **대부분은 오탐 라벨을 재현하지 않는다** — 오탐의 지배 원인은 CLI 경로의 temperature 비결정성.
- **H0 (귀무):** 오탐이 결정론 조건에서도 잔존한다 — 컨텍스트 부재(흰 모프 개체, 쳇바퀴)·confabulation
  몫이 지배하며, 결정론 전환만으로는 해소되지 않는다.

근거 사전 정보: petcam-lab `experiments/v41-shedding-ir-guard/REPORT.md` — 같은 shedding 오탐 32건을
adaptive@1080 결정론 조건(Sonnet)으로 재추론 시 v4.0/v4.1 모두 64/64 moving (오탐 재현 0). 단 그 실험은
입력 규격이 다르다(adaptive@1080 vs 이 레포 router 계약 6장@768). 이번 측정은 **이 레포 production
계약 입력**에서의 재검증이다.

## 2. Sample list — 고정

`experiments/label-determinism-remeasure/sample_list.json` (frozen 2026-07-21, 42건):

| 세트 | n | fp_label | 출처 |
|---|---|---|---|
| `shedding_fp` | 32 | shedding | petcam-lab `experiments/v41-shedding-ir-guard/sample_list_fp.json` (GT 전부 moving) |
| `drinking_fp` | 10 | drinking | 진단 문서 §5-B — `clip_vlm_jobs` 2건(07-19 감사 GT=moving 확정) + `behavior_logs` source=vlm 8건 (GT 미검증, owner 위임으로 후보 전량 포함) |

- `r2_key`·`duration_sec`은 DB SELECT 실측(2026-07-21, motion_clips 40건 + camera_clips 2건)으로 고정.
- 재현 방법: sample_list.json 그대로 사용 (스크립트가 이 파일만 읽음, DB 재조회 없음).

## 3. 모델 / 입력표현 / 프롬프트

| 항목 | 값 | 비고 |
|---|---|---|
| 모델 | `claude-sonnet-5` (exact ID) | alias 금지. `model_mismatch` 시 즉시 중단 |
| API | Anthropic Messages API (`reporter/anthropic_analyzer.py` `analyze_clip` 재사용) | `temperature=0`, `max_tokens=256`, JSON schema 강제 |
| 입력 | 시간순 JPEG 6장, 긴 변 768px no-upscale, quality 85 (`reporter/vlm_frames.extract_six`) | router 계약 `six-768q85-v1` 동일 |
| 프롬프트 | **v4.0 핀** — `reporter/prompts/system.v4.0.md` | v4.1 사용 금지 (petcam-lab 기준 reject 버전) |
| 스키마 | production main `618f4f8` 동치 7-class (basking 없음): eating_paste, eating_prey, drinking, shedding, moving, unseen, hand_feeding | 브랜치 `anthropic_analyzer.py`는 v4.1+8-class라 **파일 무수정 런타임 override**로 v4.0 프롬프트+7-class 스키마 주입 |
| duration | sample_list의 DB 실측값 | production `process_jobs`와 동일 (DB duration을 프롬프트 텍스트에 주입) |
| 반복 | 클립당 **3회** 순차 재추론 = 126 호출 | 결정론 검증 겸용 |

재시도: 일시 에러(429/5xx/connection)만 exponential backoff 최대 3회. 4xx 인증·요청 오류는 즉시 전체 중단.

## 4. 측정 지표

1. **3회 일치율** — 3회 라벨이 완전 일치한 클립 비율 (결정론 검증, 기대 ≥95%).
2. **클립별 대표 라벨** — 3회 최빈(≥2회) 라벨. 3-way 불일치는 대표 라벨 없음 + `determinism_violation` 플래그.
3. **진짜 오탐 잔존율** — true_fp 클립 수 / 42.
4. **비용** — usage(input/cache_write/cache_read/output) 실측 합산, `reporter/vlm_budget.calculate_cost`.

## 5. 판정 룰 (사전 고정)

**클립별:**
- 대표 라벨 == 원 오탐 라벨(shedding_fp→`shedding`, drinking_fp→`drinking`) → **진짜 오탐 (P2 타깃)**
- 그 외(다른 라벨이 최빈, 또는 3-way 불일치) → **비결정성 귀속 (P1로 해소)**

**전체 decision (true_fp_rate = 진짜 오탐 / 42):**

| true_fp_rate | decision |
|---|---|
| ≤ 25% | `adopt` — 결정론 전환이 주 해결책, P2는 잔존 목록만 |
| 25% 초과 ~ 50% 이하 | `hold` |
| > 50% | `reject` — 비결정성 가설 기각, 컨텍스트 문제가 지배 |

3회 일치율이 95% 미달이어도 판정 룰은 그대로 적용하고, REPORT에 원인 분석을 별도 기재한다.

## 6. 예상 비용 (사전 산출)

- 호출: 42클립 × 3회 = 126회.
- 이미지: 768×432 기준 ⌈768/28⌉×⌈432/28⌉=448 tok × 6장 ≈ 2,688 tok/호출. 텍스트 ~50 tok.
- 시스템 프롬프트 v4.0: 15,896자 ≈ ~4,500 tok — ephemeral cache 대상.
- 단가(`calculate_cost`): input $2/M, cache write $2.5/M, cache read $0.2/M, output $10/M.
- **best(캐시 적중)**: input $0.69 + cache read $0.11 + output(~150tok) $0.19 ≈ **$1.0**.
- **worst(캐시 전무)**: input $1.83 + output $0.19 ≈ **$2.1**.
- 예상 구간 **$1.0~2.1** (owner 승인 한도 ~$2.5 이내). **실측 누적 $5 초과 시 즉시 중단** (하드 게이트).

## 7. 전제조건 (preflight)

- `ANTHROPIC_API_KEY`가 레포 `.env`에 존재해야 실행. **작성 시점(2026-07-21) 부재 확인 → 키 세팅
  전까지 실행 차단** (키 생성/요청 금지 계약 — owner가 직접 세팅).
- ffmpeg/ffprobe 사용 가능, R2 자격 증명(.env) 유효.
- production 테이블 쓰기 0 (결과는 `results.json` 파일로만), LaunchAgent·plist·env 무변경.

## 8. Decision 룰 요약

§5 표 그대로. adopt/hold/reject 판정과 진짜 오탐 클립 목록(P2 스코프 입력값)을 `REPORT.md`에 기록하고
petcam-lab `docs/decision-gate.md` 회신은 후속 Task 6에서 수행.
