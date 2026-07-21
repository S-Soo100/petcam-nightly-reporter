# TEST-SHEET-B — P1 오탐 재측정 · 플랜 B (구독 CLI 3회-일치 프로토콜)

> pre-reg. **실행 전 고정 — 사후 변경 금지** (petcam-lab `.claude/rules/research-testing.md`).
> 작성: 2026-07-21. 배경: A안(TEST-SHEET.md, Messages API temp=0)은 owner 크레딧 결제가 콘솔 결함으로
> 막혀 **보류**. owner가 "크레딧 구매 없이" 진행을 지시, 약식 프로토콜 B 승인.

## 0. A안과의 관계 — 약식 대체 아님

- **B는 A안의 약식 대체가 아니다.** A안(temp=0 결정론 측정)은 결제 해소 후 **확정판으로 그대로 유효**하다.
- B의 성격: **구독 CLI(`claude -p`), temperature 비제어, 3회-일치 기반 근사 판정**. temp를 제어하지
  못하므로 "3/3 일치 = 결정론 라벨"의 보증이 없고, 결론에는 항상 **"약식(B)"** 라벨을 붙인다.
- 동결된 A안 산출물(TEST-SHEET.md · sample_list.json)은 **수정하지 않고 참조만** 한다.

## 1. 가설 (A안 §1 승계)

- **H1:** 반복 재추론에서 기존 오탐 42건의 대부분은 오탐 라벨을 안정적으로(3/3) 재현하지 않는다 —
  오탐의 지배 원인은 CLI 경로의 temperature 비결정성.
- **H0:** 오탐 라벨이 3/3로 안정 재현되는 클립이 지배적이다 — 컨텍스트 부재(흰 모프 개체,
  쳇바퀴)·confabulation 몫이 지배.

## 2. Sample list — A안 그대로 (재동결 불필요)

`experiments/label-determinism-remeasure/sample_list.json` (frozen 2026-07-21, 42건 —
shedding_fp 32 + drinking_fp 10). B는 이 파일을 read-only로 그대로 사용한다. duration_sec 포함
필드 일체 재조회·수정 없음.

## 3. 모델 / 입력표현 / 프롬프트 / 실행 방식

| 항목 | 값 | 비고 |
|---|---|---|
| 실행 | 로컬 MacBook, 구독 `claude -p` (CLI 2.1.177) | Messages API 호출 0 (키 없음 계약) |
| 모델 | `--model claude-sonnet-5` (exact ID) | alias 금지. envelope `modelUsage`에 요청 모델 부재 시 `model_mismatch` 즉시 중단 |
| temperature | **비제어** (CLI에 플래그 없음 — 진단 §3 확정) | B의 핵심 한계 |
| 입력 | 시간순 JPEG 6장, 긴 변 768px no-upscale, quality 85 (`reporter/vlm_frames.extract_six`) | A안·router 계약 `six-768q85-v1` 동일 |
| 프레임 전달 | `--add-dir` + `--tools Read --allowed-tools Read` — claude가 Read로 로컬 프레임 열람 | `reporter/classify.py`·`claude_cli_analyzer.py` production 패턴 |
| 프롬프트 | **v4.0 핀** — `--append-system-prompt-file reporter/prompts/system.v4.0.md` | v4.1 사용 금지 (canary REJECTED) |
| 출력 강제 | `--output-format json` + `--json-schema` (7-class 단일클립: eating_paste, eating_prey, drinking, shedding, moving, unseen, hand_feeding — A안 `V40_OUTPUT_SCHEMA` 동일) | `structured_output` 우선, 부재 시 result 텍스트 JSON 블록 폴백 |
| 기타 플래그 | `--safe-mode --no-session-persistence --effort low` | safe-mode=CLAUDE.md 등 커스터마이즈 차단(주입 오염 방지), effort low=production `claude_cli_batch`(drinking 오탐 생산 경로) 동일 + 쿼터 절약. 2.1.177엔 `--max-turns` 없음 → 미사용 |
| duration | sample_list의 DB 실측값을 유저 프롬프트 텍스트에 주입 | A안 §3 동일 |
| 반복 | 클립당 **3회** 순차 = 126 호출, 호출 간 1.5초 간격 | |

재시도: 클립 단위 일시 실패(timeout/봉투 파싱 실패/분류불가 rc)만 subretry 최대 2회.
**auth·quota(`is_error` 봉투 포함)·model_mismatch는 즉시 전체 중단** + 진행분 저장(resume).

## 4. 측정 지표

1. **3회 일치율** — 3회 라벨 완전 일치 클립 비율 (참고 지표 — 결정론 보증 아님).
2. **클립별 3/3 오탐 재현 여부** — 원 오탐 라벨(shedding_fp→shedding, drinking_fp→drinking) 3/3 재현.
3. **진짜 오탐(강) 비율** — 3/3 재현 클립 수 / 42.
4. **토큰** — envelope `modelUsage` 실측 합산 (input/cache_write/cache_read/output). 비용 $0 (구독).

## 5. 판정 룰 (사전 고정)

**클립별:**
- **3/3 원 오탐 라벨 재현** → **진짜 오탐(강)** — temp 비제어에서도 안정 재현 = P2 타깃 유력.
- **그 외 (1~2회 재현 또는 0회)** → **비결정성 귀속(약)** — 샘플링 변동으로 라벨이 흔들림.

**전체 decision (strong_fp_rate = 진짜 오탐(강) / 42) — A안 §5 게이트 동일:**

| strong_fp_rate | decision |
|---|---|
| ≤ 25% | `adopt (약식 B)` — 결정론 전환이 주 해결책이라는 방향 지지 |
| 25% 초과 ~ 50% 이하 | `hold (약식 B)` |
| > 50% | `reject (약식 B)` — 비결정성 가설 기각 방향 |

decision 라벨에는 항상 "약식(B)"를 표기하고, 확정 판정은 A안 실행으로 대체한다.

## 6. 한계 (사전 명시)

1. **temperature 비제어** — 3/3 일치가 결정론을 보증하지 않는다 (낮은-분산 샘플링일 뿐).
   역으로 1~2회 재현을 "비결정성 귀속"으로 분류하는 것도 근사다 — temp>0에서 진짜 오탐이
   우연히 흔들렸을 가능성을 배제 못 한다. 그래서 B의 decision은 방향 판단용, 확정은 A안.
2. **구독 한도 공유** — 이 배치는 owner 본인·워커와 같은 구독 쿼터를 쓴다(메모리
   claude-subscription-quota-shared). 한도 실패는 `is_error` 봉투로 rc 0으로 오므로
   (메모리 claude-headless-silent-quota-failure) 봉투 검사 필수, 감지 시 즉시 중단+resume.
3. CLI 경로는 agentic(Read 도구 루프)이라 Messages API 단발 호출과 입력 소비 방식이 다르다 —
   A안과 토큰·캐시 프로파일 비교는 참고만.

## 7. 예상 토큰 / 쿼터 가드

- 호출 126회. 호출당 추정: 시스템(v4.0 append ~4.5k) + CLI 기본 시스템/도구 + 이미지 6장
  (768×432 ≈ 448 tok ×6 ≈ 2.7k, Read 도구 결과로 유입) + 출력 ~150 tok.
  headless 이미지 분류 실측 기준 클립당 ~10만 토큰 수준(캐시 비중 큼 — 메모리
  claude-headless-image-cost) → **총 수백만 토큰(대부분 cache read) 예상**.
- 비용 **$0** (구독). 하드 게이트는 비용이 아니라 **한도 실패 즉시 중단** + 20클립 단위 진행 로그.
- 진행분은 클립 단위 durable 저장(`results_b.json`) — 중단 시 남은 클립만 재개.

## 8. 전제조건 (preflight — 2026-07-21 실측 완료)

- `claude auth status` loggedIn=true (terraaidev@gmail.com, claude.ai 구독) ✅
- CLI 2.1.177: `--append-system-prompt-file`/`--json-schema`/`--safe-mode`/`--tools` 지원 ✅ (`--max-turns` 없음)
- ffmpeg/ffprobe ✅, R2 자격 증명(.env) ✅
- production 테이블 쓰기 0 (DB 접근 자체 없음 — sample_list만 사용), LaunchAgent·plist·env 무변경,
  영상/프레임은 임시 디렉토리(자동 삭제).

## 9. Decision 룰 요약

§5 표 그대로. 결과는 `results_b.json` + `REPORT-B.md`에 기록. petcam-lab decision-gate 회신은
후속 Task에서 수행. A안은 결제 해소 시 이 시트와 무관하게 원안대로 실행한다.
