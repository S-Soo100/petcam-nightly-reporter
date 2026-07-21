# Nightly 행동 라벨 결정론 전환 + 오탐 재측정 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans + superpowers:systematic-debugging(Task 1) + superpowers:test-driven-development(코드 변경). Design: [`../specs/2026-07-21-nightly-label-determinism-design.md`](../specs/2026-07-21-nightly-label-determinism-design.md) — 먼저 전부 읽을 것.

**Goal:** owner-visible 야간 행동 라벨 경로를 temperature=0 결정론 경로로 전환하고, 기존 오탐(shedding·쳇바퀴 drinking)을 결정론 조건에서 재추론해 "진짜 오탐 목록"을 확정한다.

## Task 1 — 라벨 경로 진단 (read-only, 코드 수정 금지)

- [ ] `specs/next-session.md` live runtime 섹션 + Mac mini `launchctl print`로 현재 로드된 서비스 실측 (2026-07-16 기록을 정본으로 단정하지 말고 재검증)
- [ ] owner가 본 오탐 라벨(Slack 리포트의 shedding / 쳇바퀴 drinking)의 생산 코드 경로를 로그·DB로 특정: `classify.py`(claude -p)인지 candidate-worker(`claude_cli_batch`)인지
- [ ] 해당 경로의 temperature 제어 가능 여부 확정 → 진단 요약을 커밋 (문서만)

## Task 2 — 오탐 표본 수집

- [ ] shedding 오탐 세트 복원: petcam-lab 2026-07-08 재현 실험 기록(`petcam-lab/experiments/` v41-shedding 계열)에서 클립 목록 확보
- [ ] 쳇바퀴→drinking 오탐 클립 식별 — 리포트/DB에서 후보 추린 뒤 **owner 확인** (STOP)
- [ ] 표본 목록을 `experiments/` 하위에 sample_list로 고정

## Task 3 — TEST-SHEET (pre-reg, 실행 전 고정)

- [ ] H0/H1, 표본, 모델(exact ID)·프롬프트 버전(기본 v4.0 핀 — design §5)·입력 규격, 클립당 3회 재추론 일치 기준, 판정 룰(오탐 재현=진짜 오탐 / 미재현=비결정성 귀속), 예상 비용($10 cap 내) 명시
- [ ] **사용자 승인** 후 동결 (STOP)

## Task 4 — 결정론 배선 (최소 변경)

- [ ] Task 1에서 특정된 경로를 `reporter/anthropic_analyzer.py` 기반 temperature=0 경로로 전환 (TDD)
- [ ] 프롬프트 버전 명시 핀 (기본 v4.0; v4.1 유지 시 근거를 명시 결정으로 기록)
- [ ] durable 저장·cap·breaker 등 기존 제약 승계 확인. launchd/유료 활성화가 필요하면 **사용자 승인** 후 (STOP)

## Task 5 — 재추론 실행 + REPORT

- [ ] 표본 전량 3회 재추론 → 3회 일치율(결정론 검증) + 라벨 판정
- [ ] REPORT: 비결정성 귀속 vs 진짜 오탐 분리 표 + decision (petcam-lab research-testing 프로토콜)

## Task 6 — 회신·마무리

- [ ] petcam-lab `docs/decision-gate.md`에 결과 append (P2 스코프 입력값: 잔존 진짜 오탐 목록)
- [ ] 이 레포 `specs/next-session.md` 갱신, 커밋+push

## Stop rules

- Task 1에서 오탐 경로가 특정 안 되면(증거 상충) 중단하고 증거와 함께 보고
- $10 cap 잔여가 재측정 예상 비용보다 작으면 실행 전 중단 → 사용자 승인
- 운영 LaunchAgent 변경은 어떤 경우에도 사용자 승인 없이 금지
