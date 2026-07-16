# VLM 휴식(basking) 분류 복구 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude VLM이 휴식 중 작은 머리 움직임을 `moving`/`unseen`으로 강제 분류하지 않도록 기존 GT enum `basking`을 복구하고, 11개 blind canary로 검증한 뒤 기준 충족 시에만 Mac mini production에 반영한다.

**Architecture:** 저장 enum은 라벨링 정본과 같은 `basking`을 사용하고, 새 prompt `system.v4.1.md`와 모든 analyzer schema를 동일하게 유지한다. 하이라이트 등록과 Slack 표시는 기존 순수 경계에서 처리한다. 실제 Claude canary는 MacBook이 아니라 Mac mini의 격리 worktree에서 shared VLM lock과 host guard를 거친 read-only/local-media 경로로만 실행한다.

**Tech Stack:** Python 3.12, uv, pytest, Claude Code CLI structured output, LaunchAgent, git worktree

## Global Constraints

- 실행 레포는 `/Users/baek/petcam-nightly-reporter`다.
- 구현 호스트는 `BaekBook-Pro-14-M5.local`, Claude 실행 호스트는 `baeg-endeuui-Macmini.local`이다.
- production runtime label은 `com.petcam.vlm-candidate-worker`, `com.petcam.vlm-historical-backfill` 두 개다.
- 새 행동 enum을 만들지 않고 기존 `basking`을 사용한다. 사용자 표시는 `휴식`이다.
- `basking`은 자동 하이라이트 등록 제외다.
- DB migration, 기존 150건 수정, failed_terminal 재큐잉, GT/behavior_labels/app activity/Gate 변경은 금지한다.
- canary는 production DB·Slack을 쓰지 않고 11개 local mp4만 읽는다.
- MacBook에서 Claude VLM을 호출하지 않는다.
- canary 기준 미달이면 main merge·Mac mini production pull을 금지한다.
- 기존 untracked `2026-07-15-claude-cli-batch-reliability-hardening` 문서 2개를 add·수정·삭제하지 않는다.

---

## File Map

- Create: `reporter/prompts/system.v4.1.md` — `basking` 경계가 추가된 새 prompt provenance
- Modify: `reporter/claude_cli_analyzer.py` — CLI batch schema와 v4.1 prompt 경로
- Modify: `reporter/anthropic_analyzer.py` — 직접 API 비활성 상태의 schema/prompt parity
- Modify: `reporter/classify.py` — legacy CLI adapter의 prompt 경로 parity
- Modify: `reporter/config.py` — prompt version과 기본 skip actions
- Modify: `reporter/vlm_run_summary.py` — `basking → 휴식` 집계
- Modify: `reporter/register.py` — 설명 주석만 정합
- Create: `tests/test_vlm_basking_contract.py` — prompt/schema/provenance 계약
- Modify: `tests/test_claude_cli_analyzer.py` — CLI가 `basking` 응답을 수용하는 회귀
- Modify: `tests/test_register.py` — `basking` 등록 제외
- Modify: `tests/test_vlm_run_summary.py` — Slack `휴식` 표시
- Create: `experiments/vlm-basking-20260716/human-blind-manifest.json` — short ID·기대 행동·제품 제외만 보존
- Create: `scripts/evaluate_vlm_basking_canary.py` — host/lock fail-closed local-media canary
- Create: `tests/test_evaluate_vlm_basking_canary.py` — manifest, batching, 수용 기준, no-write 경계
- Modify: `specs/next-session.md` — 결과와 운영 상태
- Modify: `.claude/donts-audit.md` — 분류 공백·단일 호스트 교훈

---

### Task 1: `basking` prompt·schema·provenance 계약

**Files:**
- Create: `reporter/prompts/system.v4.1.md`
- Create: `tests/test_vlm_basking_contract.py`
- Modify: `tests/test_claude_cli_analyzer.py`
- Modify: `reporter/claude_cli_analyzer.py`
- Modify: `reporter/anthropic_analyzer.py`
- Modify: `reporter/classify.py`
- Modify: `reporter/config.py`

**Interfaces:**
- Produces: analyzer action enum에 `basking`, `VLM_PROMPT_VERSION == "v4.1-direct-images"`
- Preserves: `CliBatchResult`, `BatchOutcome`, retry/breaker/clip-set/exact-model 계약

- [ ] **Step 1: 새 prompt/schema 계약의 실패 테스트를 작성한다**

`tests/test_vlm_basking_contract.py`를 다음 계약으로 만든다.

```python
from pathlib import Path

from reporter import anthropic_analyzer, classify, config
from reporter import claude_cli_analyzer as cli


def test_basking_is_canonical_in_every_vlm_schema():
    assert "basking" in cli._SCHEMA["properties"]["items"]["items"]["properties"]["action"]["enum"]
    assert "basking" in anthropic_analyzer.OUTPUT_SCHEMA["properties"]["action"]["enum"]


def test_all_analyzers_use_v41_prompt_and_provenance():
    assert cli._SYSTEM_FILE.name == "system.v4.1.md"
    assert classify._SYSTEM_FILE.name == "system.v4.1.md"
    assert "system.v4.1.md" in str(anthropic_analyzer.SYSTEM_PROMPT_PATH)
    assert config.VLM_PROMPT_VERSION == "v4.1-direct-images"


def test_prompt_defines_basking_moving_unseen_boundary():
    prompt = Path(cli._SYSTEM_FILE).read_text()
    required = (
        "basking",
        "head/eye/gaze",
        "body position",
        "Do NOT infer `moving` merely because the motion-triggered camera recorded the clip",
        "partially occluded",
    )
    assert all(text in prompt for text in required)
```

`anthropic_analyzer.py`에는 test가 안전하게 경로를 확인할 수 있도록 `SYSTEM_PROMPT_PATH` 상수를 노출한다.

- [ ] **Step 2: RED를 확인한다**

Run:

```bash
cd /Users/baek/petcam-nightly-reporter
uv run pytest tests/test_vlm_basking_contract.py -q
```

Expected: `basking` 누락 또는 `system.v4.1.md` 부재로 FAIL.

- [ ] **Step 3: prompt v4.1과 schema를 최소 변경한다**

`system.v4.0.md`를 복사해 `system.v4.1.md`를 만들고 다음 내용을 정확히 반영한다.

```text
# Behavior classes (choose ONE)
- eating_paste
- eating_prey
- drinking
- shedding
- basking
- moving
- unseen
- hand_feeding
```

Decision rule과 `available_classes`에 다음 의미를 넣는다.

```text
`basking` = resting in one place without locomotion. Small head/eye/gaze movements,
looking around, breathing, or a minor posture adjustment while the torso remains in
the same location are still `basking`. A partially occluded gecko is `basking` when
its identifiable visible parts show this resting pattern. Do NOT infer `moving`
merely because the motion-triggered camera recorded the clip.

`moving` = the torso or whole-body position actually changes relative to the
enclosure background, including walking, climbing, jumping, or clear locomotion.
Head-only scanning at the same location is not `moving`.
```

우선순위는 다음처럼 고정한다.

```text
hand_feeding > eating_prey > eating_paste > drinking > shedding > moving > basking > unseen
```

기존 prompt에서 `head movement`를 일반이동으로 보는 문장, enclosure 안에 있기만 해도 `moving`으로 보는 문장, 식별 가능한 신체 일부가 조금만 움직여도 `moving`으로 보는 문장을 새 경계 규칙으로 교체한다.

Python 변경은 다음 계약을 따른다.

```python
_SYSTEM_FILE = Path(__file__).parent / "prompts" / "system.v4.1.md"
_ACTIONS = [
    "eating_paste", "eating_prey", "drinking", "shedding",
    "basking", "moving", "unseen", "hand_feeding",
]
```

`anthropic_analyzer.py`:

```python
SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts/system.v4.1.md"
SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text()
```

직접 API `OUTPUT_SCHEMA` action enum도 CLI와 같은 8개로 만든다. 직접 API 호출 경로·비용 설정은 바꾸지 않는다.

`config.py`:

```python
VLM_PROMPT_VERSION = "v4.1-direct-images"
```

- [ ] **Step 4: CLI `basking` 응답 수용 회귀를 추가한다**

`tests/test_claude_cli_analyzer.py`에 fake runner 기반 테스트를 추가한다.

```python
def test_cli_batch_accepts_basking_result(tmp_path):
    items = [
        {"clip_id": "c1", "action": "basking", "confidence": 0.91,
         "reasoning": "The torso stays in place while the head scans."},
        {"clip_id": "c2", "action": "moving", "confidence": 0.88,
         "reasoning": "The torso changes position."},
    ]

    def runner(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(_envelope(items)),
            stderr="",
        )

    result = analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=runner)
    assert result.results["c1"]["action"] == "basking"
    assert result.results["c2"]["action"] == "moving"
```

- [ ] **Step 5: Task 1 테스트를 통과시킨다**

Run:

```bash
uv run pytest tests/test_vlm_basking_contract.py tests/test_claude_cli_analyzer.py -q
```

Expected: 모두 PASS, provider 실제 호출 0회.

- [ ] **Step 6: Task 1을 커밋한다**

```bash
git add reporter/prompts/system.v4.1.md reporter/claude_cli_analyzer.py \
  reporter/anthropic_analyzer.py reporter/classify.py reporter/config.py \
  tests/test_vlm_basking_contract.py tests/test_claude_cli_analyzer.py
git commit -m "feat: VLM 휴식 basking 분류 계약 복구"
```

---

### Task 2: 하이라이트 등록 제외와 Slack 표시

**Files:**
- Modify: `reporter/config.py`
- Modify: `reporter/register.py`
- Modify: `reporter/vlm_run_summary.py`
- Modify: `tests/test_register.py`
- Modify: `tests/test_vlm_run_summary.py`

**Interfaces:**
- Consumes: Task 1의 action `basking`
- Produces: `should_register("basking") is False`, 정규 Slack 행동 분포 `휴식 N`

- [ ] **Step 1: 등록·Slack 실패 테스트를 먼저 작성한다**

`tests/test_register.py::test_should_register_filters_noninformative`에 다음 assertion을 추가한다.

```python
assert register.should_register("basking") is False
```

`tests/test_vlm_run_summary.py`에 다음 테스트를 추가한다.

```python
def test_format_basking_as_korean_rest_without_collapsing_to_other():
    msg = format_vlm_run_summary(_summary(action_dist={"basking": 2, "moving": 1}))
    assert "· 행동: 휴식 2 · 일반이동 1" in msg
    assert "기타" not in msg
```

- [ ] **Step 2: RED를 확인한다**

Run:

```bash
uv run pytest tests/test_register.py::test_should_register_filters_noninformative \
  tests/test_vlm_run_summary.py::test_format_basking_as_korean_rest_without_collapsing_to_other -q
```

Expected: skip action 또는 Slack allowlist 누락으로 FAIL.

- [ ] **Step 3: 최소 구현한다**

`config.py`의 기본값만 다음처럼 확장한다.

```python
os.environ.get(
    "REGISTER_SKIP_ACTIONS",
    "moving,basking,error,unseen,shedding",
).split(",")
```

`register.py` docstring도 `basking(휴식)`이 비정보성 자동 등록 제외임을 명시한다. 환경변수 override 동작은 바꾸지 않는다.

`vlm_run_summary.py`:

```python
_ACTION_ALLOWLIST = (
    "eating_paste", "eating_prey", "drinking", "shedding",
    "basking", "moving", "unseen", "hand_feeding",
)

_ACTION_LABEL = {
    # 기존 항목 유지
    "basking": "휴식",
}
```

- [ ] **Step 4: 관련 테스트와 회귀를 통과시킨다**

Run:

```bash
uv run pytest tests/test_register.py tests/test_vlm_run_summary.py \
  tests/test_vlm_backfill_summary.py -q
```

Expected: 모두 PASS. rolling backfill Slack 형식은 행동 분포를 새로 추가하지 않고 기존 전문 유지.

- [ ] **Step 5: Task 2를 커밋한다**

```bash
git add reporter/config.py reporter/register.py reporter/vlm_run_summary.py \
  tests/test_register.py tests/test_vlm_run_summary.py
git commit -m "fix: 휴식 영상 하이라이트 제외와 Slack 표시 정합"
```

---

### Task 3: 11개 blind manifest와 Mac mini 전용 canary

**Files:**
- Create: `experiments/vlm-basking-20260716/human-blind-manifest.json`
- Create: `scripts/evaluate_vlm_basking_canary.py`
- Create: `tests/test_evaluate_vlm_basking_canary.py`

**Interfaces:**
- Consumes: `extract_six(video, out_dir)`, `analyze_batch_with_retry(frame_sets, model)`, `require_expected_host(actual, expected)`, shared `acquire_vlm_lock()`
- Produces: exit 0인 안전 요약 JSON 또는 기준 미달·infra 실패 nonzero

- [ ] **Step 1: 사람 blind manifest를 작성한다**

다음 JSON을 정확히 기록한다. 전체 UUID·reasoning·영상은 넣지 않는다.

```json
{
  "version": "vlm-basking-canary-v1",
  "recorded_before_model_retry": true,
  "cases": [
    {"clip8":"9e05ad4c","filename":"04-9e05ad4c.mp4","expected_action":"unseen","product_outcome":null},
    {"clip8":"4e99ed40","filename":"01-4e99ed40.mp4","expected_action":"unseen","product_outcome":null},
    {"clip8":"9d1d2cfb","filename":"03-9d1d2cfb.mp4","expected_action":"moving","product_outcome":null},
    {"clip8":"ab273d21","filename":"02-ab273d21.mp4","expected_action":"basking","product_outcome":"exclude"},
    {"clip8":"ad1772c6","filename":"08-ad1772c6.mp4","expected_action":"basking","product_outcome":null},
    {"clip8":"941aadb9","filename":"05-941aadb9.mp4","expected_action":"basking","product_outcome":null},
    {"clip8":"ab8cd4b0","filename":"06-ab8cd4b0.mp4","expected_action":"basking","product_outcome":"exclude"},
    {"clip8":"1d34eb48","filename":"07-1d34eb48.mp4","expected_action":"basking","product_outcome":null},
    {"clip8":"a3774a4f","filename":"11-a3774a4f.mp4","expected_action":"unseen","product_outcome":null},
    {"clip8":"ca27e1f3","filename":"09-ca27e1f3.mp4","expected_action":"moving","product_outcome":null},
    {"clip8":"864c45da","filename":"10-864c45da.mp4","expected_action":"moving","product_outcome":null}
  ]
}
```

- [ ] **Step 2: canary 순수 경계의 실패 테스트를 작성한다**

`tests/test_evaluate_vlm_basking_canary.py`는 최소 다음을 고정한다.

```python
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from reporter.claude_cli_analyzer import BatchOutcome, CliBatchResult
from reporter.vlm_budget import Usage
from reporter.vlm_host_guard import HostOwnershipError, require_expected_host
from scripts.evaluate_vlm_basking_canary import (
    CanaryCase,
    CanarySummary,
    accepted,
    evaluate_cases,
    execute_canary,
    load_manifest,
)

MANIFEST = (
    Path(__file__).parents[1]
    / "experiments/vlm-basking-20260716/human-blind-manifest.json"
)


def fake_cases():
    actions = ["unseen"] * 3 + ["moving"] * 3 + ["basking"] * 5
    return tuple(
        CanaryCase(f"{index:08x}", f"{index:02d}-{index:08x}.mp4", action, None)
        for index, action in enumerate(actions, start=1)
    )


def fake_videos(tmp_path, cases):
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    for case in cases:
        (video_dir / case.filename).write_bytes(b"fake-video")
    return video_dir


def fake_extract(_video, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(6):
        frame = out_dir / f"f_{index}.jpg"
        frame.write_bytes(b"jpeg")
        frames.append(frame)
    return frames


def fake_analyzer(calls, expected):
    def analyze(frame_sets, model):
        calls.append(tuple(frame_sets))
        results = {
            clip8: {
                "clip_id": clip8,
                "action": expected[clip8],
                "confidence": 0.9,
                "reasoning": "must not be persisted",
            }
            for clip8 in frame_sets
        }
        result = CliBatchResult(
            "req", model, model, results, Usage(0, 0, 0, 0), 0.0, False
        )
        return BatchOutcome(result, 1, False, None, None)
    return analyze


def test_manifest_has_11_unique_cases_and_expected_distribution():
    cases = load_manifest(MANIFEST)
    assert len(cases) == 11
    assert len({case.clip8 for case in cases}) == 11
    assert Counter(case.expected_action for case in cases) == {
        "unseen": 3, "moving": 3, "basking": 5,
    }
    assert sum(case.product_outcome == "exclude" for case in cases) == 2


def test_acceptance_requires_all_infra_terminal_and_label_thresholds():
    passing = CanarySummary(total=11, infra_failed=0, exact_by_action={
        "unseen": 3, "moving": 3, "basking": 4,
    }, visible_basking_as_unseen=0)
    assert accepted(passing) is True
    assert accepted(replace(passing, infra_failed=1)) is False
    assert accepted(replace(passing, visible_basking_as_unseen=1)) is False
    assert accepted(replace(passing, exact_by_action={
        "unseen": 3, "moving": 3, "basking": 3,
    })) is False


def test_batches_are_4_4_3_and_output_omits_reasoning(tmp_path):
    calls = []
    cases = fake_cases()
    expected = {case.clip8: case.expected_action for case in cases}
    result = evaluate_cases(
        cases=cases, video_dir=fake_videos(tmp_path, cases),
        model="claude-sonnet-5",
        analyzer=fake_analyzer(calls, expected), extract_fn=fake_extract,
    )
    assert [len(call) for call in calls] == [4, 4, 3]
    assert "reasoning" not in json.dumps(result.to_dict())


def test_host_or_lock_failure_happens_before_auth_and_analyzer():
    calls = []
    with pytest.raises(HostOwnershipError):
        execute_canary(
            cases=fake_cases(), video_dir=Path("/unused"),
            model="claude-sonnet-5", actual_host="wrong.local",
            expected_host="baeg-endeuui-Macmini.local",
            host_guard_fn=require_expected_host,
            auth_fn=lambda: calls.append("auth"),
            lock_fn=lambda: calls.append("lock"),
            release_fn=lambda _lock: calls.append("release"),
            analyzer=lambda *_args: calls.append("analyze"),
            extract_fn=lambda *_args: calls.append("extract"),
        )
    assert calls == []


def test_lock_busy_prevents_auth_frames_and_claude():
    calls = []
    with pytest.raises(RuntimeError, match="vlm_lock_busy"):
        execute_canary(
            cases=fake_cases(), video_dir=Path("/unused"),
            model="claude-sonnet-5",
            actual_host="baeg-endeuui-Macmini.local",
            expected_host="baeg-endeuui-Macmini.local",
            host_guard_fn=require_expected_host,
            auth_fn=lambda: calls.append("auth"), lock_fn=lambda: None,
            release_fn=lambda _lock: calls.append("release"),
            analyzer=lambda *_args: calls.append("analyze"),
            extract_fn=lambda *_args: calls.append("extract"),
        )
    assert calls == []
```

- [ ] **Step 3: RED를 확인한다**

Run:

```bash
uv run pytest tests/test_evaluate_vlm_basking_canary.py -q
```

Expected: script와 함수가 없어 FAIL.

- [ ] **Step 4: canary script를 최소 구현한다**

공개 인터페이스는 다음 이름과 인자 계약으로 고정한다.

```python
@dataclass(frozen=True, slots=True)
class CanaryCase:
    clip8: str
    filename: str
    expected_action: str
    product_outcome: str | None


@dataclass(frozen=True, slots=True)
class CanarySummary:
    total: int
    infra_failed: int
    exact_by_action: dict[str, int]
    visible_basking_as_unseen: int
    rows: list[dict[str, object]] = field(default_factory=list)
```

- `load_manifest(path: Path) -> Sequence[CanaryCase]`
- `evaluate_cases(cases, video_dir, model, *, analyzer, extract_fn) -> CanarySummary`
- `execute_canary(cases, video_dir, model, *, actual_host, expected_host, host_guard_fn, auth_fn, lock_fn, release_fn, analyzer, extract_fn) -> CanarySummary`
- `main(argv=None, *, actual_host_fn=socket.gethostname, host_guard=require_expected_host, lock_fn=acquire_vlm_lock, auth_fn=check_cli_auth) -> int`

`accepted`는 다음 순수 구현을 사용한다.

```python
def accepted(summary: CanarySummary) -> bool:
    exact = summary.exact_by_action
    return (
        summary.total == 11
        and summary.infra_failed == 0
        and exact.get("unseen", 0) == 3
        and exact.get("moving", 0) == 3
        and exact.get("basking", 0) >= 4
        and summary.visible_basking_as_unseen == 0
    )
```

`execute_canary`의 guard/lock 구현은 다음으로 고정한다.

```python
def execute_canary(cases, video_dir, model, *, actual_host, expected_host,
                   host_guard_fn, auth_fn, lock_fn, release_fn,
                   analyzer, extract_fn):
    host_guard_fn(actual_host, expected_host)
    lock = lock_fn()
    if lock is None:
        raise RuntimeError("vlm_lock_busy")
    try:
        auth_fn()
        return evaluate_cases(
            cases, video_dir, model, analyzer=analyzer, extract_fn=extract_fn
        )
    finally:
        release_fn(lock)
```

구현 규칙:

1. host guard → shared lock → manifest/video 검증 → auth → frame 추출 → 4/4/3 분석 순서다.
2. host/lock 실패면 auth·frame·Claude 호출 전 nonzero다.
3. manifest는 정확히 11개, short ID는 `^[0-9a-f]{8}$`, 행동은 `{unseen,moving,basking}`, filename basename만 허용한다.
4. manifest filename과 video directory의 파일이 1:1 일치해야 한다. 누락·중복이면 Claude 호출 전 실패한다.
5. 각 batch는 `analyze_batch_with_retry`를 사용하고 기존 최대 2 subattempt를 넘지 않는다.
6. `TemporaryDirectory` 아래에만 frame을 만들고 항상 정리한다.
7. production DB client, Slack module, `update_job`, selector 함수를 import하지 않는다.
8. output JSON에는 clip8, expected, predicted, confidence, match, aggregate만 넣고 reasoning·전체 UUID·경로를 넣지 않는다.
9. `accepted()`는 total=11, infra_failed=0, unseen exact=3, moving exact=3, basking exact≥4, visible basking→unseen=0일 때만 true다.
10. `--output` 파일을 쓰고 accepted면 0, 기준 미달이면 2, infra/입력 오류면 1을 반환한다.

- [ ] **Step 5: 구체 테스트를 GREEN으로 만든다**

Run:

```bash
uv run pytest tests/test_evaluate_vlm_basking_canary.py -q
```

Expected: 모든 테스트 PASS, 실제 Claude/DB/Slack 호출 0회.

- [ ] **Step 6: Task 3을 커밋한다**

```bash
git add experiments/vlm-basking-20260716/human-blind-manifest.json \
  scripts/evaluate_vlm_basking_canary.py tests/test_evaluate_vlm_basking_canary.py
git commit -m "test: 휴식 분류 blind canary 추가"
```

---

### Task 4: 전체 회귀·정적 안전 감사·문서화

**Files:**
- Modify: `specs/next-session.md`
- Modify: `.claude/donts-audit.md`

**Interfaces:**
- Consumes: Task 1~3 전체
- Produces: feature branch canary 실행 준비 완료 상태

- [ ] **Step 1: 전체 테스트를 실행한다**

```bash
cd /Users/baek/petcam-nightly-reporter
uv run pytest
```

Expected: 기존 272개와 신규 테스트 전부 PASS.

- [ ] **Step 2: syntax·whitespace를 검증한다**

```bash
uv run python -m compileall reporter scripts
git diff --check
```

Expected: exit 0.

- [ ] **Step 3: 금지 동작을 정적으로 감사한다**

```bash
rg -n "create_client|update_job|behavior_labels|behavior_logs|send_slack|send_vlm_run_summary" \
  scripts/evaluate_vlm_basking_canary.py
```

Expected: 0건. canary script는 DB·Slack write 경로를 import하지 않는다.

```bash
git status --short
```

Expected: 7월 15일 기존 미추적 문서 2개는 계속 `??`, 이번 변경 파일만 tracked modification/add 상태다. 두 기존 문서는 stage하지 않는다.

- [ ] **Step 4: SOT에 구현 전 상태를 기록한다**

`specs/next-session.md`에 다음 사실을 적는다.

- v4.1 `basking` 계약 구현 및 local tests 결과
- 기존 11건 blind 분포 3/3/5와 제품 제외 2건
- Claude canary는 Mac mini 전용이고 아직 production main 미반영
- DB migration과 기존 150건 재분류 없음

`.claude/donts-audit.md`에는 다음 교훈을 한 줄로 남긴다.

```text
VLM enum에 GT 정본 행동이 없으면 모델 오류율로 해석하지 않는다. taxonomy gap을 먼저 닫고, 구현 host와 Claude runtime host를 분리한 blind canary로 검증한다.
```

- [ ] **Step 5: 문서 커밋 후 feature branch를 push한다**

```bash
git add specs/next-session.md .claude/donts-audit.md
git commit -m "docs: VLM 휴식 분류 canary 운영 계약 기록"
git push -u origin feat/vlm-basking-classification
FEATURE_SHA="$(git rev-parse HEAD)"
test "$(git rev-parse --verify HEAD)" = "$FEATURE_SHA"
```

Expected: feature branch가 origin과 IN SYNC. main은 아직 변경하지 않는다.

---

### Task 5: Mac mini 격리 canary와 조건부 production 반영

**Files:**
- Runtime output only: `/Users/baek-end/petcam-nightly-reporter-vlm-basking-canary/storage/result.json`
- Local audit copy: `/Users/baek/petcam-lab/storage/retry-review-20260711/vlm-basking-v4.1-canary.json`

**Interfaces:**
- Consumes: feature branch의 정확한 `FEATURE_SHA`, local mp4 11개
- Produces: 수용 기준 통과 시 main fast-forward와 Mac mini production main pull

- [ ] **Step 1: Mac mini production 상태를 read-only로 고정한다**

```bash
FEATURE_SHA="$(git -C /Users/baek/petcam-nightly-reporter rev-parse HEAD)"
ssh home-mac 'hostname; git -C ~/petcam-nightly-reporter rev-parse HEAD; \
  launchctl print gui/$(id -u)/com.petcam.vlm-candidate-worker 2>/dev/null | grep -E "state =|last exit code"; \
  launchctl print gui/$(id -u)/com.petcam.vlm-historical-backfill 2>/dev/null | grep -E "state =|last exit code"'
```

Expected: hostname `baeg-endeuui-Macmini.local`, 두 service loaded, production repo는 기존 main SHA. 실행 중이면 종료될 때까지 canary를 시작하지 않는다.

- [ ] **Step 2: Mac mini에 feature SHA 격리 worktree를 만든다**

```bash
ssh home-mac "git -C ~/petcam-nightly-reporter fetch origin && \
  git -C ~/petcam-nightly-reporter worktree add --detach \
  ~/petcam-nightly-reporter-vlm-basking-canary $FEATURE_SHA"
```

Expected: canary worktree HEAD가 `$FEATURE_SHA`, production main working directory 불변.

- [ ] **Step 3: 11개 영상만 canary storage로 복사한다**

```bash
ssh home-mac 'mkdir -p ~/petcam-nightly-reporter-vlm-basking-canary/storage/input'
rsync -av --include='*.mp4' --exclude='*' \
  /Users/baek/petcam-lab/storage/retry-review-20260711/ \
  home-mac:~/petcam-nightly-reporter-vlm-basking-canary/storage/input/
ssh home-mac 'find ~/petcam-nightly-reporter-vlm-basking-canary/storage/input \
  -maxdepth 1 -type f -name "*.mp4" | wc -l'
```

Expected: 정확히 `11`.

- [ ] **Step 4: Mac mini에서만 canary를 실행한다**

```bash
ssh home-mac 'cd ~/petcam-nightly-reporter-vlm-basking-canary && \
  VLM_EXPECTED_HOST=baeg-endeuui-Macmini.local \
  uv run python scripts/evaluate_vlm_basking_canary.py \
    --manifest experiments/vlm-basking-20260716/human-blind-manifest.json \
    --video-dir storage/input \
    --model claude-sonnet-5 \
    --output storage/result.json'
```

Expected: host guard와 shared lock 통과, 4/4/3 batch, exit 0. lock 경합이면 Claude 0회로 종료하고 자연 worker가 끝난 뒤 한 번만 재실행한다.

- [ ] **Step 5: 결과를 회수하고 사람 기준과 대조한다**

```bash
scp home-mac:~/petcam-nightly-reporter-vlm-basking-canary/storage/result.json \
  /Users/baek/petcam-lab/storage/retry-review-20260711/vlm-basking-v4.1-canary.json
jq '{total,infra_failed,exact_by_action,visible_basking_as_unseen,accepted}' \
  /Users/baek/petcam-lab/storage/retry-review-20260711/vlm-basking-v4.1-canary.json
```

Required:

```json
{
  "total": 11,
  "infra_failed": 0,
  "exact_by_action": {"unseen": 3, "moving": 3, "basking": 4},
  "visible_basking_as_unseen": 0,
  "accepted": true
}
```

`basking` exact는 4 이상이면 통과한다. 결과가 기준 미달이면 여기서 중단하고 main·production을 변경하지 않는다.

- [ ] **Step 6: canary media와 worktree를 정리한다**

```bash
ssh home-mac 'find ~/petcam-nightly-reporter-vlm-basking-canary/storage/input \
  -type f -name "*.mp4" -delete; \
  find ~/petcam-nightly-reporter-vlm-basking-canary -type f \
  \( -name "*.mp4" -o -name "*.jpg" -o -name "*.jpeg" \) | wc -l'
```

Expected: `0`.

결과 JSON을 이미 MacBook으로 회수했으므로 격리 worktree도 제거한다.

```bash
ssh home-mac 'git -C ~/petcam-nightly-reporter worktree remove \
  ~/petcam-nightly-reporter-vlm-basking-canary'
```

- [ ] **Step 7: 통과한 경우에만 main을 fast-forward하고 push한다**

MacBook:

```bash
cd /Users/baek/petcam-nightly-reporter
git switch main
git pull --ff-only origin main
git merge --ff-only feat/vlm-basking-classification
git push origin main
MAIN_SHA="$(git rev-parse HEAD)"
test "$MAIN_SHA" = "$FEATURE_SHA"
```

Expected: main == origin/main == `$FEATURE_SHA`, force push 없음.

- [ ] **Step 8: Mac mini production main을 안전하게 갱신한다**

두 LaunchAgent가 `state = running`이면 끝날 때까지 기다린다. bootout/reinstall하지 않는다.

```bash
ssh home-mac "git -C ~/petcam-nightly-reporter pull --ff-only origin main && \
  test \"\$(git -C ~/petcam-nightly-reporter rev-parse HEAD)\" = '$FEATURE_SHA'"
```

Expected: production working directory HEAD가 `$FEATURE_SHA`, plist/env/label 불변.

- [ ] **Step 9: 다음 자연 cycle을 검증한다**

Backfill 다음 `:35`, 정규 다음 `22:00/00:00/02:00/04:00` 이후 다음을 확인한다.

```bash
ssh home-mac 'launchctl print gui/$(id -u)/com.petcam.vlm-candidate-worker 2>/dev/null | grep -E "last exit code|runs ="; \
  launchctl print gui/$(id -u)/com.petcam.vlm-historical-backfill 2>/dev/null | grep -E "last exit code|runs ="; \
  find ~/petcam-nightly-reporter/storage /tmp -type f \
  \( -name "*.mp4" -o -name "*.jpg" -o -name "*.jpeg" \) 2>/dev/null | wc -l'
```

Required: exit 0, selector crossover 0, succeeded replay 0, temp media 0, schema/parser error 0. 자연 표본에 `basking`이 없으면 Slack `휴식` 실사 표시는 아직 관측 전이라고 보고한다.

- [ ] **Step 10: 최종 SOT를 갱신하고 커밋·push한다**

`specs/next-session.md`에 canary confusion 결과, production SHA, 두 LaunchAgent 상태, 자연 cycle 결과, 미검증 항목을 기록한다.

```bash
git add specs/next-session.md
git commit -m "docs: VLM 휴식 분류 canary 및 운영 반영 결과"
git push origin main
git status --short
```

Expected: 기존 미추적 7월 15일 문서 2개만 남고 이번 tracked 변경은 clean.

---

## Final Report Contract

Claude는 다음을 표로 보고한다.

1. 변경 파일과 commit SHA
2. v4.1 행동 enum·prompt SHA·schema parity
3. 11건 사람 판정 ↔ 변경 전 retry ↔ v4.1 canary 1:1 결과
4. unseen 3, moving 3, basking 5의 정확도와 오분류 목록
5. 제품 제외 2건이 행동 라벨과 분리됐는지
6. 하이라이트 `basking` 등록 0 증거
7. full pytest·compileall·diff-check 결과
8. Mac mini hostname·production repo HEAD·두 LaunchAgent 상태
9. selector crossover·succeeded replay·중복·직접 API 비용·temp media 수
10. 실제 자연 cycle에서 검증된 항목과 아직 관측 전인 항목
