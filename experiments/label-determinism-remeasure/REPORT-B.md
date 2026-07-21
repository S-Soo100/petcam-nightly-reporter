# REPORT-B — P1 오탐 재측정 · 플랜 B (구독 CLI 3회-일치 약식)

> TEST-SHEET-B: [`TEST-SHEET-B.md`](TEST-SHEET-B.md) (pre-reg `f1f541e`, 2026-07-21).
> 실행: 2026-07-21 13:43~14:21 KST, 로컬 MacBook, `claude` CLI 2.1.177 구독(`claude-sonnet-5` exact),
> 프롬프트 v4.0, 6장@768q85, 42클립 × 3회 = 126콜 **완주**. 원자료: `results_b.json`.
> ⚠️ **약식(B)** — temperature 비제어. 확정판은 A안(TEST-SHEET.md, Messages API temp=0) 결제 해소 후.

## 1. 결과 요약

| 지표 | 값 |
|---|---|
| 완주 | **126/126 콜, 42/42 클립** (한도/인증 실패 0, is_error 0) |
| 3회 일치율 (unanimous) | **35/42 = 83.3%** (참고 지표 — 결정론 보증 아님) |
| **진짜 오탐(강)** — 원 오탐 라벨 3/3 재현 | **0/42 = 0%** (shedding 0/32, drinking 0/10) |
| 비결정성 귀속(약) | 42/42 |
| **decision (사전 게이트 ≤25% adopt)** | **`adopt` (약식 B)** |
| 토큰 (126콜 합산) | input 756 + cache_write 839,231 + cache_read 5,195,836 + output 136,809 ≈ **6.17M** |
| 비용 | $0 (구독) |

**126런 라벨 분포:** moving 70 · unseen 48 · drinking 4 · shedding 4. confidence 0.35~0.9.

## 2. 클립별 결과 표 (원 오탐 라벨 → 3회 라벨)

| clip | set | fp_label | GT | 3회 라벨 | 재현 | outcome |
|---|---|---|---|---|---|---|
| `09309a08` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `12d105df` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `2780fa10` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `2b619572` | shedding_fp | shedding | moving | moving / drinking / moving | 0/3 | nondeterminism_weak |
| `2f045137` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `384af925` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `3a52b1fa` | shedding_fp | shedding | moving | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `3c8001b8` | shedding_fp | shedding | moving | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `3e51c7ed` | shedding_fp | shedding | moving | drinking / drinking / drinking | 0/3 | nondeterminism_weak |
| `43d9daba` | shedding_fp | shedding | moving | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `68c8c236` | shedding_fp | shedding | moving | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `696e0e44` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `745c11bb` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `748c1b7d` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `75f784f3` | shedding_fp | shedding | moving | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `7b3e66f7` | shedding_fp | shedding | moving | unseen / moving / moving | 0/3 | nondeterminism_weak |
| `84ca0e44` | shedding_fp | shedding | moving | **shedding** / unseen / moving | 1/3 | nondeterminism_weak |
| `88c41a95` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `922a1cba` | shedding_fp | shedding | moving | **shedding / shedding** / moving | 2/3 | nondeterminism_weak |
| `9789c34f` | shedding_fp | shedding | moving | unseen / unseen / moving | 0/3 | nondeterminism_weak |
| `9cc31d6f` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `9e6b6a69` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `b02ab63e` | shedding_fp | shedding | moving | unseen / **shedding** / moving | 1/3 | nondeterminism_weak |
| `bfb7156c` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `c8dd5dbd` | shedding_fp | shedding | moving | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `ddc4eb01` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `dfb3d43e` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `e5e24602` | shedding_fp | shedding | moving | moving / unseen / moving | 0/3 | nondeterminism_weak |
| `e5e75a3e` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `e5edf886` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `fb0328cb` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `fb59b152` | shedding_fp | shedding | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `135c6248` | drinking_fp | drinking | — | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `25ee99b0` | drinking_fp | drinking | — | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `29a74166` | drinking_fp | drinking | — | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `3db14864` | drinking_fp | drinking | — | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `439e1798` | drinking_fp | drinking | — | moving / moving / moving | 0/3 | nondeterminism_weak |
| `6c16a62b` | drinking_fp | drinking | moving | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `a57ce7cd` | drinking_fp | drinking | — | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `ad4bd25e` | drinking_fp | drinking | — | unseen / unseen / unseen | 0/3 | nondeterminism_weak |
| `b0171f2d` | drinking_fp | drinking | moving | moving / moving / moving | 0/3 | nondeterminism_weak |
| `e679f8ad` | drinking_fp | drinking | — | unseen / unseen / unseen | 0/3 | nondeterminism_weak |

## 3. 시험지 대비 — **사후 변경 없음**

표본(A안 sample_list 42건)·모델(exact sonnet-5)·프롬프트(v4.0)·입력(6장@768q85)·판정 룰(3/3=강,
게이트 ≤25%/≤50%/>50%)·플래그 구성 전부 TEST-SHEET-B(`f1f541e`) 그대로. 합격 기준 변경 없음.

## 4. 가설 판정 — H1 지지 (약식)

- **진짜 오탐(강) 0건.** shedding 오탐 32건 중 **어느 것도 3/3 재현 없음** (부분 재현 3클립뿐 —
  `922a1cba` 2/3, `84ca0e44`·`b02ab63e` 1/3, 전체 96런 중 shedding 4런 = 4.2%).
- **drinking 오탐 10건은 30런 중 drinking 0런** — 단 1회도 재현 안 됨.
- 비-unanimous 7클립(16.7%) = 같은 입력·같은 호출에 라벨이 흔들리는 **temp>0 비결정성의 직접 증거**.
  petcam-lab v41-shedding-ir-guard(결정론 조건 64/64 moving, 재현 0)와 방향 일치 — 이번엔
  production 계약 입력(6장@768)에서 재확인.
- **H1(오탐의 지배 원인 = temperature 비결정성) 지지, H0 기각 방향** — 단 약식(B) 한계 내에서.

## 5. decision — **`adopt` (약식 B)**

strong_fp_rate 0% ≤ 25%. 결정론 전환(P1, Messages API temp=0 배선)이 주 해결책이라는 방향을
지지한다. **진짜 오탐(강) 잔존 목록 = 없음 → P2(컨텍스트 보강) 스코프 입력값 없음** (약식 기준).
확정 판정·P2 최종 스코프는 A안(temp=0) 실행으로 대체한다.

## 6. 한계 · 노이즈 · 관찰

1. **temperature 비제어** (TEST-SHEET-B §6 사전 명시) — "3/3 일치=결정론" 보증 없음. 역으로 강 0건도
   temp>0 흔들림 덕에 과소평가됐을 가능성은 낮지만(재현률 자체가 4/126런) 배제 불가. 확정은 A안.
2. **unseen 48/126런 (38%)** — 6장@768 입력에서 게코 식별 실패가 많다. 원 오탐 라벨과 GT(moving)
   양쪽 모두와 다른 제3 라벨로 흔들린 경우가 다수 = 이 입력 표현의 정보 부족 신호. drinking FP
   10건 중 8건이 unseen 3/3인 점은 "쳇바퀴/움직임을 drinking으로 확신"했던 원 라벨이 얼마나
   불안정한 기반이었는지 보여줌.
3. **신규 오분류 후보**: `3e51c7ed`(GT moving)가 drinking 3/3 — shedding→drinking으로 옮겨간
   안정적 오분류. A안·후속 GT 정제에서 주목 대상.
4. **실행 사고(결과 무영향)**: 13:39~43 KST 사이 외부 요인으로 1차 배치 진행분(results_b.json,
   untracked)이 삭제되고 프로세스가 종료됨 → 동일 프로토콜로 재실행해 완주. 커밋된 파일은 무손상
   (TEST-SHEET-B.md mtime만 갱신 = 내용 동일). 판정에 쓰인 데이터는 전부 완주 배치(126콜)의 것.
   추가 소모 콜 ~120회(집계 밖, 구독이라 $0). 원인 미특정 — 같은 워킹트리 동시 세션 정리 작업 추정.
5. 원 오탐 생산 경로는 2계열(classify.py alias `sonnet`=4-6 / cli_batch sonnet-5)인데 재측정은
   sonnet-5 단일 — shedding 32건(4-6 산) 재현률에 모델 차이가 섞였을 수 있음 (A안도 동일 조건).

## 7. 다음 액션

1. **P1 결정론 배선 방향 유지** — plan Task 4 (`VLM_PROVIDER=direct_api` 전환, launchd + 유료 활성화
   = owner 승인 게이트). A안은 결제 해소 후 확정판 실행.
2. petcam-lab `docs/decision-gate.md` 회신 (plan Task 6, 별도 세션).
3. P2 컨텍스트 보강: 약식 기준 강 잔존 0이라 **보류** — A안 확정 후 재판단.
