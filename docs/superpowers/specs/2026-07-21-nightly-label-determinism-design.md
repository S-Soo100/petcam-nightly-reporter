# Nightly 행동 라벨 결정론 전환 + 오탐 재측정 — Design

> 발주: petcam-lab 2026-07-21 연구방향 상담 P1 (decision-gate 로그 `petcam-lab/docs/decision-gate.md` 2026-07-21 레코드, adopt).
> 성격: 신규 설계가 아니라 **이미 설계·부분배포된 결정론 경로(Anthropic Messages API, temperature=0)를 owner-visible 라벨 생산 경로에 배선**하고, 그 상태에서 기존 오탐을 재측정하는 작업.

## 1. 문제

owner가 보는 야간 행동 라벨(Slack 리포트)이 비결정적이다.

- **탈피 오탐**: 야간 IR 클립을 shedding으로 라벨. petcam-lab 재현 실험(2026-07-08)에서 같은 오탐 32건을 adaptive@1080 결정론 조건으로 재추론하니 v4.0·v4.1 모두 64/64 moving → **원인은 프롬프트/모델이 아니라 temperature 비결정성**. (개체가 릴리화이트+아잔틱 모프라 흰무늬 = 상시 오탐 위험이라는 별도 컨텍스트 요인도 있음 — 이건 P2 영역.)
- **쳇바퀴→drinking 오탐 (신규)**: 쳇바퀴 반복 동작이 v4.0 drinking 정의("몸 고정+머리 반복 핥기")에 걸림.
- 근본: `claude -p`(Claude CLI)는 temperature 제어 옵션 자체가 없다. 반면 이 레포에는 이미 `reporter/anthropic_analyzer.py`(Messages API, `temperature=0`, structured output, cost 추적)가 존재한다 — budgeted VLM candidate router v1 설계의 산물.

## 2. 목표

1. **owner-visible 행동 라벨을 만드는 실제 경로**를 특정하고(추정 금지 — 아래 §4 진단 선행), 그 경로를 temperature=0 결정론 경로로 전환한다.
2. 전환된 결정론 조건에서 **기존 오탐 세트를 재추론**해 "비결정성 몫"과 "진짜 오탐(컨텍스트 부재 등)"을 분리한 목록을 만든다.
3. 잔존 진짜 오탐 목록을 petcam-lab `docs/decision-gate.md`에 회신 append한다 — **P2(케이지 프로필 메타) 착수/폐기의 스코프 입력값**이다.

## 3. 비목표

- 프롬프트 내용 수정 (버전 선택은 §5 참조 — 내용 수정은 별도 실험 체계 사안)
- P2(케이지 프로필 메타) 구현
- selector/슬롯 정책, budget cap, backfill 로직 변경
- `behavior_logs`·앱 하이라이트·고객 알림 반영 범위 변경

## 4. 진단 선행 (추측 구현 금지)

owner가 본 오탐 라벨이 어느 경로 산출물인지 증거로 특정한다. 후보:

| 경로 | 호출 방식 | temperature | 비고 |
|---|---|---|---|
| `reporter/classify.py` (`com.petcam.nightly-reporter` 리포트 경로 추정) | `claude -p` CLI | 제어 불가 | 오탐 발생 경로로 추정되나 검증 필요 |
| `com.petcam.vlm-candidate-worker` | provider `claude_cli_batch` | 제어 불가 | shadow — owner-visible인지 확인 필요 |
| `reporter/anthropic_analyzer.py` | Messages API | **0** | 결정론 경로. 현재 어느 서비스가 쓰는지 확인 필요 |

증거: Mac mini `launchctl print`, 각 서비스 로그, Slack 리포트의 라벨 출처 필드, DB(`clip_vlm_jobs` 등) 대조. `specs/next-session.md` 2026-07-16 live runtime 재검증 섹션이 출발점.

## 5. 프롬프트 버전 결정 (함정)

`anthropic_analyzer.py`는 현재 `prompts/system.v4.1.md`를 로드한다. **petcam-lab 기준 v4.1은 shedding 오탐 맥락에서 reject된 버전이고 v4.0이 기준선이다** (petcam-lab 메모리/실험 기록). 결정론 전환 시 프롬프트 버전은 **v4.0으로 핀**하는 것을 기본값으로 하되, 이 레포에서 v4.1을 쓰는 별도 근거가 문서에 있으면 그 근거와 함께 명시 결정으로 남긴다. 암묵 승계 금지.

## 6. 재측정 = 의사결정용 테스트

잔존 오탐 목록은 petcam-lab P2의 착수 여부를 정하는 의사결정 입력이므로 **TEST-SHEET(실행 전 고정) + REPORT(decision) 의무** — petcam-lab `.claude/rules/research-testing.md` 프로토콜 준수.

- 표본: ① 2026-07-08 shedding 오탐 32건 세트(petcam-lab `experiments/` 재현 기록에서 복원) ② 최근 쳇바퀴→drinking 오탐 클립(owner에게 클립 식별 요청) ③ 가능하면 최근 N일 리포트의 케어 라벨 전수.
- 방법: 결정론 경로(temperature=0, v4.0, 동일 입력 규격)로 클립당 3회 재추론 → 3회 일치 확인(결정론 검증) + 라벨 판정.
- 판정 룰(사전 고정): 재추론 라벨이 오탐을 재현하면 "진짜 오탐"(P2 타깃), moving 등으로 돌아오면 "비결정성 귀속"(P1로 해소).
- 비용: Messages API 유료 — 기존 월 $10 hard cap 원장 안에서 집행, TEST-SHEET에 예상 비용 명시. cap 충돌 시 표본 축소가 아니라 **사용자 승인** 먼저.

## 7. 제약 승계

이 레포 기존 global constraints 전부 유지: durable 저장 전 API 호출 금지, 모델 exact ID(`claude-sonnet-5`), 월 $10 cap, shadow 경계(`behavior_logs`/하이라이트/알림 불변), launchd 변경·유료 활성화는 사용자 승인 후, `uv add`만, 비밀값 `.env`만. `com.petcam.activity-worker` 불변.

## 8. 완료 조건

- [ ] §4 진단: owner-visible 라벨 경로 특정 (증거 포함)
- [ ] 결정론 배선: 해당 경로가 temperature=0 + 명시 프롬프트 버전으로 라벨 생산 (승인 게이트 준수)
- [ ] TEST-SHEET → 재추론 → REPORT (decision + 잔존 진짜 오탐 목록)
- [ ] petcam-lab `docs/decision-gate.md`에 결과 회신 append + 이 레포 `specs/next-session.md` 갱신
