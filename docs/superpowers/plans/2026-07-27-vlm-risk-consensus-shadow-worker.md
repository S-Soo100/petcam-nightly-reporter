# VLM Risk Consensus Shadow Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 첫 Claude CLI batch에 위험 라벨이 있을 때 같은 frame·prompt·model·clip 순서로 두 번 더 판독하고, production 결과를 바꾸지 않은 채 세 attempt를 shadow 원장에 저장한다.

**Architecture:** 판정·payload 생성은 새 순수 모듈, Supabase RPC는 새 store adapter, 실행 orchestration만 기존 `process_cli_jobs`에 연결한다. feature flag가 꺼지거나 첫 결과가 전부 `moving`이면 기존 호출·DB update 경로를 그대로 유지하며, shadow는 첫 결과가 production에 저장된 뒤에만 실행된다.

**Tech Stack:** Python 3.12, Claude CLI batch adapter, Supabase RPC, launchd, pytest

## Global Constraints

- provider는 `claude_cli_batch`, exact model은 `claude-sonnet-5`다.
- prompt는 `v4.0-direct-images`, sampler는 `six-768q85-v1` 그대로다.
- 위험 actions는 `eating_paste`, `eating_prey`, `drinking`, `shedding`, `unseen`, `hand_feeding`이다.
- 첫 결과가 위험이면 같은 ready batch 전체를 attempt 2·3에 재사용한다.
- frame map 객체·clip 순서·prompt·model을 바꾸지 않고 R2 download·frame extraction을 추가하지 않는다.
- shadow 단계에서 다수결 결과를 만들거나 production job result/status를 덮어쓰지 않는다.
- 앱·Slack·GT·behavior·activity·selector write는 0이다.
- reasoning, frame path, R2 key, signed URL, 이메일, raw provider request id는 원장·로그에 남기지 않는다.
- `_SHADOW_TWO_CALL_BUDGET = 1260 seconds`로 사전 등록한다: `2 calls × (2 subattempts × 300s timeout + 30s buffer)`.
- feature flag 기본값은 `false`; production LaunchAgent의 명시적 `1` 없이는 호출하지 않는다.
- production migration apply, main merge, Mac mini install/run은 이 구현 계획 범위 밖이다.

---

## File Structure

- Create: `reporter/vlm_consensus_shadow.py`
  - 위험 판정, canonical batch hash, 안전한 attempt payload를 만든다.
- Create: `reporter/vlm_shadow_store.py`
  - `fn_insert_vlm_shadow_attempt_batch` RPC를 호출하고 count drift를 fail-closed 처리한다.
- Create: `tests/test_vlm_consensus_shadow.py`
  - 순수 판정·hash·payload·비밀 필드 부재를 검증한다.
- Create: `tests/test_vlm_shadow_store.py`
  - RPC 이름·payload·오류 위생을 검증한다.
- Modify: `reporter/config.py`
  - 기본 false flag와 protocol version을 추가한다.
- Modify: `reporter/vlm_candidate_worker.py`
  - 첫 결과 저장 후 조건부 shadow attempt 2·3을 실행한다.
- Create: `tests/test_vlm_consensus_shadow_worker.py`
  - worker의 동일 batch, deadline, breaker, production 불변을 검증한다.
- Modify: `install-launchd-vlm-candidate.sh`
  - 명시적 shadow flag를 plist에 전달한다.
- Modify: `tests/test_install_vlm_launchd.py`
  - 기본 false와 명시적 true 렌더링을 검증한다.
- Modify: `specs/next-session.md`
  - 구현 상태와 배포 금지 경계를 additive로 기록한다.
- Modify: `.claude/donts-audit.md`
  - shadow가 primary 결과보다 우선하지 않는다는 교훈을 한 줄 기록한다.
- Create: `docs/handoff-prompts/2026-07-27-vlm-risk-consensus-shadow-worker-report.md`
  - 구현·회귀·금지동작 증거를 기록한다.

### Task 1: 순수 위험 판정과 attempt payload

**Files:**
- Create: `tests/test_vlm_consensus_shadow.py`
- Create: `reporter/vlm_consensus_shadow.py`

**Interfaces:**
- Produces:
  - `RISK_ACTIONS: frozenset[str]`
  - `PROTOCOL_VERSION: str`
  - `should_trigger_shadow(results: dict[str, dict]) -> bool`
  - `batch_identity_sha256(jobs: list[dict], protocol_version: str) -> str`
  - `success_rows(*, jobs: list[dict], attempt_index: int, batch_hash: str, model_actual: str, provider_request_id: str, results: dict[str, dict], usage: Usage, provider_estimated_cost_usd: float) -> list[dict]`
  - `failure_rows(*, jobs: list[dict], attempt_index: int, batch_hash: str, status: str, failure_code: str, model_actual: str | None = None) -> list[dict]`
  - `ShadowIntegrityError`

- [ ] **Step 1: 순수 계약의 failing tests를 작성한다**

```python
import json

import pytest

from reporter.vlm_budget import Usage
from reporter.vlm_consensus_shadow import (
    PROTOCOL_VERSION,
    ShadowIntegrityError,
    batch_identity_sha256,
    failure_rows,
    should_trigger_shadow,
    success_rows,
)


def jobs():
    return [
        {
            "id": "job-a", "clip_id": "clip-a", "model_requested": "claude-sonnet-5",
            "prompt_version": "v4.0-direct-images", "prompt_sha256": "a" * 64,
            "sampler_version": "six-768q85-v1",
        },
        {
            "id": "job-b", "clip_id": "clip-b", "model_requested": "claude-sonnet-5",
            "prompt_version": "v4.0-direct-images", "prompt_sha256": "a" * 64,
            "sampler_version": "six-768q85-v1",
        },
    ]


def test_only_non_moving_actions_trigger():
    assert should_trigger_shadow({"clip-a": {"action": "drinking"}})
    assert should_trigger_shadow({"clip-a": {"action": "unseen"}})
    assert not should_trigger_shadow({"clip-a": {"action": "moving"}})
    with pytest.raises(ShadowIntegrityError):
        should_trigger_shadow({"clip-a": {"action": "unknown"}})


def test_batch_hash_is_order_sensitive_and_64hex():
    one = batch_identity_sha256(jobs(), PROTOCOL_VERSION)
    two = batch_identity_sha256(list(reversed(jobs())), PROTOCOL_VERSION)
    assert len(one) == 64 and int(one, 16) >= 0
    assert one != two


def test_success_payload_excludes_reasoning_and_raw_request_id():
    rows = success_rows(
        jobs=jobs(),
        attempt_index=1,
        batch_hash=batch_identity_sha256(jobs(), PROTOCOL_VERSION),
        model_actual="claude-sonnet-5",
        provider_request_id="raw-session-secret",
        results={
            "clip-a": {"action": "drinking", "confidence": 0.8, "reasoning": "raw"},
            "clip-b": {"action": "moving", "confidence": 0.7, "reasoning": "raw"},
        },
        usage=Usage(10, 2, 3, 4),
        provider_estimated_cost_usd=0.2,
    )
    serialized = json.dumps(rows)
    assert "reasoning" not in serialized
    assert "raw-session-secret" not in serialized
    assert [row["batch_position"] for row in rows] == [0, 1]
    assert sum(row["input_tokens"] for row in rows) == 10


def test_failure_payload_has_no_action_or_confidence():
    rows = failure_rows(
        jobs=jobs(), attempt_index=2,
        batch_hash=batch_identity_sha256(jobs(), PROTOCOL_VERSION),
        status="deferred", failure_code="shadow_deferred_deadline",
    )
    assert all(row["action"] is None and row["confidence"] is None for row in rows)
```

- [ ] **Step 2: RED를 확인한다**

Run: `uv run pytest -q tests/test_vlm_consensus_shadow.py`

Expected: FAIL because `reporter.vlm_consensus_shadow` does not exist.

- [ ] **Step 3: 최소 순수 구현을 작성한다**

```python
from __future__ import annotations

import hashlib
import json

from reporter.vlm_budget import Usage

PROTOCOL_VERSION = "risk-consensus-shadow-v1"
RISK_ACTIONS = frozenset({
    "eating_paste", "eating_prey", "drinking", "shedding", "unseen", "hand_feeding",
})
ALL_ACTIONS = RISK_ACTIONS | {"moving"}
FAILURE_CODES = frozenset({
    "shadow_deferred_deadline", "not_logged_in", "auth_probe_failed", "quota_exceeded",
    "shadow_provider_error", "shadow_model_mismatch", "shadow_clip_set_mismatch",
    "shadow_not_run_breaker",
})


class ShadowIntegrityError(RuntimeError):
    pass


def should_trigger_shadow(results: dict[str, dict]) -> bool:
    actions = [item.get("action") for item in results.values()]
    if not actions or any(action not in ALL_ACTIONS for action in actions):
        raise ShadowIntegrityError("invalid shadow action set")
    return any(action in RISK_ACTIONS for action in actions)


def batch_identity_sha256(jobs: list[dict], protocol_version: str = PROTOCOL_VERSION) -> str:
    canonical = {
        "protocol_version": protocol_version,
        "members": [
            {"position": i, "job_id": job["id"], "clip_id": job["clip_id"]}
            for i, job in enumerate(jobs)
        ],
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _split(total: int, count: int, index: int) -> int:
    q, r = divmod(int(total), count)
    return q + (1 if index < r else 0)


def _base(job: dict, position: int, count: int, attempt: int, batch_hash: str) -> dict:
    return {
        "job_id": job["id"], "clip_id": job["clip_id"],
        "protocol_version": PROTOCOL_VERSION,
        "batch_identity_sha256": batch_hash,
        "batch_size": count, "batch_position": position, "attempt_index": attempt,
        "provider": "claude_cli_batch", "model_requested": job["model_requested"],
        "prompt_version": job["prompt_version"], "prompt_sha256": job["prompt_sha256"],
        "sampler_version": job["sampler_version"],
    }


def success_rows(*, jobs: list[dict], attempt_index: int, batch_hash: str,
                 model_actual: str, provider_request_id: str,
                 results: dict[str, dict], usage: Usage,
                 provider_estimated_cost_usd: float) -> list[dict]:
    if set(results) != {job["clip_id"] for job in jobs}:
        raise ShadowIntegrityError("shadow clip set mismatch")
    count = len(jobs)
    request_hash = hashlib.sha256(provider_request_id.encode()).hexdigest()
    rows = []
    for index, job in enumerate(jobs):
        item = results[job["clip_id"]]
        if item.get("action") not in ALL_ACTIONS:
            raise ShadowIntegrityError("invalid shadow action")
        rows.append({
            **_base(job, index, count, attempt_index, batch_hash),
            "status": "succeeded", "failure_code": None,
            "action": item["action"], "confidence": float(item["confidence"]),
            "model_actual": model_actual, "provider_request_sha256": request_hash,
            "input_tokens": _split(usage.input_tokens, count, index),
            "cache_creation_input_tokens": _split(
                usage.cache_creation_input_tokens, count, index
            ),
            "cache_read_input_tokens": _split(usage.cache_read_input_tokens, count, index),
            "output_tokens": _split(usage.output_tokens, count, index),
            "provider_estimated_cost_usd": provider_estimated_cost_usd / count,
        })
    return rows


def failure_rows(*, jobs: list[dict], attempt_index: int, batch_hash: str,
                 status: str, failure_code: str,
                 model_actual: str | None = None) -> list[dict]:
    if status not in {"deferred", "failed", "integrity_failure", "not_run"}:
        raise ShadowIntegrityError("invalid shadow failure status")
    if failure_code not in FAILURE_CODES:
        raise ShadowIntegrityError("invalid shadow failure code")
    return [
        {
            **_base(job, index, len(jobs), attempt_index, batch_hash),
            "status": status, "failure_code": failure_code,
            "action": None, "confidence": None, "model_actual": model_actual,
            "provider_request_sha256": None, "input_tokens": None,
            "cache_creation_input_tokens": None, "cache_read_input_tokens": None,
            "output_tokens": None, "provider_estimated_cost_usd": None,
        }
        for index, job in enumerate(jobs)
    ]
```

- [ ] **Step 4: GREEN을 확인한다**

Run: `uv run pytest -q tests/test_vlm_consensus_shadow.py`

Expected: PASS.

- [ ] **Step 5: Task 1을 커밋한다**

```bash
git add reporter/vlm_consensus_shadow.py tests/test_vlm_consensus_shadow.py
git commit -m "feat: 위험 라벨 shadow 판정 계약 추가"
```

### Task 2: Supabase shadow store adapter

**Files:**
- Create: `tests/test_vlm_shadow_store.py`
- Create: `reporter/vlm_shadow_store.py`

**Interfaces:**
- Consumes: Task 1의 `list[dict]` payload.
- Produces: `insert_attempt_batch(sb, rows: list[dict]) -> int`,
  `ShadowStoreError`, `ShadowStoreDivergence`.

- [ ] **Step 1: RPC·오류 위생 failing tests를 작성한다**

```python
import pytest

from reporter.vlm_shadow_store import (
    ShadowStoreDivergence,
    ShadowStoreError,
    insert_attempt_batch,
)


class Result:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class SB:
    def __init__(self, response=2, fail=False):
        self.response = response
        self.fail = fail
        self.calls = []

    def rpc(self, name, args):
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("password=hunter2 https://secret.supabase.co")
        return Result(self.response)


ROWS = [{"job_id": "a"}, {"job_id": "b"}]


def test_insert_calls_atomic_rpc_once():
    sb = SB()
    assert insert_attempt_batch(sb, ROWS) == 2
    assert sb.calls == [("fn_insert_vlm_shadow_attempt_batch", {"p_attempts": ROWS})]


def test_count_drift_fails_closed():
    with pytest.raises(ShadowStoreDivergence):
        insert_attempt_batch(SB(response=1), ROWS)


def test_raw_db_error_is_not_leaked():
    with pytest.raises(ShadowStoreError) as error:
        insert_attempt_batch(SB(fail=True), ROWS)
    assert "hunter2" not in str(error.value)
    assert "supabase.co" not in str(error.value)
```

- [ ] **Step 2: RED를 확인한다**

Run: `uv run pytest -q tests/test_vlm_shadow_store.py`

Expected: FAIL because the adapter does not exist.

- [ ] **Step 3: 최소 adapter를 작성한다**

```python
class ShadowStoreError(RuntimeError):
    pass


class ShadowStoreDivergence(ShadowStoreError):
    pass


def insert_attempt_batch(sb, rows: list[dict]) -> int:
    if not 1 <= len(rows) <= 4:
        raise ValueError("shadow attempt batch requires 1..4 rows")
    try:
        data = sb.rpc(
            "fn_insert_vlm_shadow_attempt_batch", {"p_attempts": rows}
        ).execute().data
    except Exception as exc:
        raise ShadowStoreError("shadow_store_failed") from exc
    if not isinstance(data, int) or data != len(rows):
        raise ShadowStoreDivergence("shadow_store_count_mismatch")
    return data
```

- [ ] **Step 4: GREEN을 확인한다**

Run: `uv run pytest -q tests/test_vlm_shadow_store.py`

Expected: PASS.

- [ ] **Step 5: Task 2를 커밋한다**

```bash
git add reporter/vlm_shadow_store.py tests/test_vlm_shadow_store.py
git commit -m "feat: VLM shadow 원장 RPC adapter 추가"
```

### Task 3: feature flag와 LaunchAgent 전달

**Files:**
- Modify: `reporter/config.py`
- Modify: `tests/test_install_vlm_launchd.py`
- Modify: `install-launchd-vlm-candidate.sh`

**Interfaces:**
- Produces:
  - `config.VLM_CONSENSUS_SHADOW_ENABLED: bool`
  - `config.VLM_CONSENSUS_SHADOW_PROTOCOL_VERSION: str`
  - plist env `VLM_CONSENSUS_SHADOW_ENABLED`

- [ ] **Step 1: 기본 false·명시 true failing tests를 추가한다**

```python
def test_candidate_installer_defaults_consensus_shadow_off(tmp_path):
    result = _run(_install_env(tmp_path))
    assert result.returncode == 0
    payload = plistlib.loads(
        (tmp_path / "Library/LaunchAgents/com.petcam.vlm-candidate-worker.plist").read_bytes()
    )
    assert payload["EnvironmentVariables"]["VLM_CONSENSUS_SHADOW_ENABLED"] == "0"


def test_candidate_installer_renders_explicit_consensus_shadow_on(tmp_path):
    result = _run(_install_env(tmp_path, VLM_CONSENSUS_SHADOW_ENABLED="1"))
    assert result.returncode == 0
    payload = plistlib.loads(
        (tmp_path / "Library/LaunchAgents/com.petcam.vlm-candidate-worker.plist").read_bytes()
    )
    assert payload["EnvironmentVariables"]["VLM_CONSENSUS_SHADOW_ENABLED"] == "1"


def test_candidate_installer_rejects_invalid_consensus_shadow_value(tmp_path):
    assert _run(_install_env(tmp_path, VLM_CONSENSUS_SHADOW_ENABLED="yes")).returncode != 0
```

- [ ] **Step 2: RED를 확인한다**

Run: `uv run pytest -q tests/test_install_vlm_launchd.py`

Expected: FAIL because the plist omits the flag.

- [ ] **Step 3: config와 installer를 최소 수정한다**

Add to `reporter/config.py`:

```python
VLM_CONSENSUS_SHADOW_ENABLED = (
    os.environ.get("VLM_CONSENSUS_SHADOW_ENABLED", "0") == "1"
)
VLM_CONSENSUS_SHADOW_PROTOCOL_VERSION = "risk-consensus-shadow-v1"
```

Add to `install-launchd-vlm-candidate.sh` before plist rendering:

```bash
CONSENSUS_SHADOW="${VLM_CONSENSUS_SHADOW_ENABLED:-0}"
[[ "$CONSENSUS_SHADOW" == "0" || "$CONSENSUS_SHADOW" == "1" ]] || {
  echo "VLM_CONSENSUS_SHADOW_ENABLED must be 0 or 1" >&2
  exit 1
}
```

Add this exact key/value inside `EnvironmentVariables`:

```xml
<key>VLM_CONSENSUS_SHADOW_ENABLED</key><string>$CONSENSUS_SHADOW</string>
```

- [ ] **Step 4: GREEN과 shell lint를 확인한다**

Run:

```bash
uv run pytest -q tests/test_install_vlm_launchd.py
bash -n install-launchd-vlm-candidate.sh
```

Expected: PASS and exit 0.

- [ ] **Step 5: Task 3을 커밋한다**

```bash
git add reporter/config.py install-launchd-vlm-candidate.sh \
  tests/test_install_vlm_launchd.py
git commit -m "feat: VLM consensus shadow 실행 스위치 추가"
```

### Task 4: 정규 worker에 동일 batch shadow 연결

**Files:**
- Create: `tests/test_vlm_consensus_shadow_worker.py`
- Modify: `reporter/vlm_candidate_worker.py`

**Interfaces:**
- Consumes: Tasks 1~3, `analyze_batch_with_retry`, 기존 `frames` dict와 `ready` order.
- Produces: feature-enabled risk batch의 attempt 1~3 ledger rows.
- Preserves: 기존 `clip_vlm_jobs` primary result/status, Slack summary, selector.

- [ ] **Step 1: disabled·moving·risk·deadline·integrity failing tests를 작성한다**

테스트는 기존 `tests.test_vlm_worker._cli_job`, `_six`, `_ok_batch`, `FakeSB` 패턴을
복사하지 말고 import 가능한 공용 fixture가 없으므로 이 파일 안에 최소 fixture를 명시한다.

```python
from datetime import datetime, timedelta, timezone

import pytest

from reporter.claude_cli_analyzer import CliBatchResult
from reporter.vlm_budget import Usage
from reporter.vlm_candidate_worker import process_cli_jobs
from reporter.vlm_shadow_store import ShadowStoreDivergence
from tests._fakes import FakeSB


def job(i: int) -> dict:
    return {
        "id": f"j{i}", "clip_id": f"c{i}", "camera_id": "cam",
        "selector_run_id": "run", "slot": "customer_highlight",
        "status": "queued", "attempt_count": 0,
        "model_requested": "claude-sonnet-5",
        "prompt_version": "v4.0-direct-images",
        "prompt_sha256": "a" * 64,
        "sampler_version": "six-768q85-v1",
    }


def sb_and_jobs() -> tuple[FakeSB, list[dict]]:
    jobs = [job(0), job(1)]
    clips = [{"id": f"c{i}", "r2_key": f"c{i}.mp4"} for i in range(2)]
    return FakeSB({"clip_vlm_jobs": jobs, "motion_clips": clips}), jobs


def result(frame_sets, action: str, request: str) -> CliBatchResult:
    items = {
        clip_id: {
            "clip_id": clip_id, "action": action, "confidence": 0.8,
            "reasoning": "must not persist",
        }
        for clip_id in frame_sets
    }
    return CliBatchResult(
        request, "claude-sonnet-5", "claude-sonnet-5",
        items, Usage(10, 0, 0, 2), 0.0, False,
    )


def run_case(actions, *, enabled, deadline=None, clock=None, store=None):
    sb, jobs = sb_and_jobs()
    calls, downloads, extracts, writes = [], [], [], []

    def analyzer(frame_sets, model):
        calls.append((id(frame_sets), tuple(frame_sets), model))
        return result(frame_sets, actions[len(calls) - 1], f"request-{len(calls)}")

    def record_store(_sb, rows):
        writes.append(rows)
        if store is not None:
            return store(_sb, rows)
        return len(rows)

    output = process_cli_jobs(
        sb, jobs, analyzer=analyzer, auth_check=lambda: None,
        download_fn=lambda key, dest: downloads.append(key) or dest,
        extract_fn=lambda video, out: extracts.append(str(video)) or [
            out / f"{i}.jpg" for i in range(6)
        ],
        deadline=deadline, clock=clock,
        shadow_enabled=enabled, shadow_store_fn=record_store,
    )
    return sb, output, calls, downloads, extracts, writes


def test_disabled_is_byte_equivalent_single_call_no_shadow_write():
    sb, output, calls, downloads, extracts, writes = run_case(["drinking"], enabled=False)
    assert len(calls) == 1 and writes == []
    assert len(downloads) == 2 and len(extracts) == 2
    assert all(row["status"] == "succeeded" for row in sb.store["clip_vlm_jobs"])


def test_all_moving_does_not_trigger_shadow():
    _, _, calls, _, _, writes = run_case(["moving"], enabled=True)
    assert len(calls) == 1 and writes == []


def test_risk_reuses_same_batch_and_writes_three_attempts():
    sb, _, calls, downloads, extracts, writes = run_case(
        ["drinking", "moving", "drinking"], enabled=True
    )
    assert len(calls) == 3
    assert calls[0][0] == calls[1][0] == calls[2][0]
    assert calls[0][1:] == calls[1][1:] == calls[2][1:]
    assert len(downloads) == 2 and len(extracts) == 2
    assert [[row["attempt_index"] for row in batch] for batch in writes] == [
        [1, 1], [2, 2], [3, 3]
    ]
    assert "reasoning" not in str(writes)
    assert all(row["result"]["action"] == "drinking" for row in sb.store["clip_vlm_jobs"])


def test_deadline_defers_both_extra_attempts_without_extra_call():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    _, _, calls, _, _, writes = run_case(
        ["drinking"], enabled=True,
        deadline=now + timedelta(seconds=1259), clock=lambda: now,
    )
    assert len(calls) == 1
    assert [batch[0]["status"] for batch in writes] == ["succeeded", "deferred", "deferred"]


def test_shadow_store_divergence_leaves_primary_succeeded_and_raises():
    sb, jobs = sb_and_jobs()

    def analyzer(frame_sets, model):
        return result(frame_sets, "drinking", "request")

    def bad_store(_sb, _rows):
        raise ShadowStoreDivergence("count mismatch")

    with pytest.raises(ShadowStoreDivergence):
        process_cli_jobs(
            sb, jobs, analyzer=analyzer, auth_check=lambda: None,
            download_fn=lambda key, dest: dest,
            extract_fn=lambda video, out: [out / f"{i}.jpg" for i in range(6)],
            shadow_enabled=True, shadow_store_fn=bad_store,
        )
    assert all(row["status"] == "succeeded" for row in sb.store["clip_vlm_jobs"])
```

- [ ] **Step 2: RED를 확인한다**

Run: `uv run pytest -q tests/test_vlm_consensus_shadow_worker.py`

Expected: FAIL because `process_cli_jobs` has no shadow seams.

- [ ] **Step 3: worker에 최소 orchestration을 추가한다**

Import Task 1·2 interfaces and add constants:

```python
from reporter.vlm_consensus_shadow import (
    batch_identity_sha256,
    failure_rows,
    should_trigger_shadow,
    success_rows,
)
from reporter.vlm_shadow_store import insert_attempt_batch

_SHADOW_TWO_CALL_BUDGET = timedelta(seconds=1260)
_SHADOW_ONE_CALL_BUDGET = timedelta(seconds=630)
```

Extend the signature without changing existing callers:

```python
def process_cli_jobs(
    sb, jobs, *, analyzer=analyze_batch, download_fn=r2.download_clip,
    extract_fn=extract_six, auth_check=check_cli_auth, sleep=lambda _s: None,
    deadline=None, clock=None, shadow_enabled=None,
    shadow_store_fn=insert_attempt_batch,
):
```

Resolve the flag once:

```python
shadow_enabled = (
    config.VLM_CONSENSUS_SHADOW_ENABLED
    if shadow_enabled is None else shadow_enabled
)
```

In `run()`, make the existing `next_run` the default CLI deadline while preserving injected
two-argument `process_fn` seams:

```python
if process_fn is None:
    if config.VLM_PROVIDER == "claude_cli_batch":
        process_fn = lambda store, due: process_cli_jobs(
            store, due, deadline=next_run
        )
    else:
        process_fn = lambda store, due: process_jobs(
            store, due, client or Anthropic()
        )
```

After the existing loop has written every primary result for the ready batch, preserve the existing
primary model breaker first, then execute the shadow sequence in the same `TemporaryDirectory` scope:

```python
if result.model_mismatch:
    breaker = "model"
    continue

if not shadow_enabled or not should_trigger_shadow(result.results):
    continue

batch_hash = batch_identity_sha256(ready, config.VLM_CONSENSUS_SHADOW_PROTOCOL_VERSION)
shadow_store_fn(sb, success_rows(
    jobs=ready, attempt_index=1, batch_hash=batch_hash,
    model_actual=result.model_actual,
    provider_request_id=result.provider_request_id,
    results=result.results, usage=result.usage,
    provider_estimated_cost_usd=result.provider_estimated_cost_usd,
))

if deadline is not None and clock() + _SHADOW_TWO_CALL_BUDGET > deadline:
    for attempt_index in (2, 3):
        shadow_store_fn(sb, failure_rows(
            jobs=ready, attempt_index=attempt_index, batch_hash=batch_hash,
            status="deferred", failure_code="shadow_deferred_deadline",
        ))
    continue

for attempt_index in (2, 3):
    if (
        attempt_index == 3 and deadline is not None
        and clock() + _SHADOW_ONE_CALL_BUDGET > deadline
    ):
        shadow_store_fn(sb, failure_rows(
            jobs=ready, attempt_index=3, batch_hash=batch_hash,
            status="deferred", failure_code="shadow_deferred_deadline",
        ))
        break

    shadow_outcome = analyze_batch_with_retry(
        frames, config.VLM_MODEL, analyzer=analyzer, sleep=sleep
    )
    if shadow_outcome.error is not None:
        exc = shadow_outcome.error
        code = {
            "not_logged_in": "not_logged_in",
            "auth_probe_failed": "auth_probe_failed",
            "quota_exceeded": "quota_exceeded",
            "clip_set_mismatch": "shadow_clip_set_mismatch",
        }.get(exc.code, "shadow_provider_error")
        status = "integrity_failure" if code == "shadow_clip_set_mismatch" else "failed"
        shadow_store_fn(sb, failure_rows(
            jobs=ready, attempt_index=attempt_index, batch_hash=batch_hash,
            status=status, failure_code=code,
        ))
        shadow_breaker = _breaker_for(exc)
        if shadow_breaker is not None:
            breaker = shadow_breaker
            if attempt_index == 2:
                shadow_store_fn(sb, failure_rows(
                    jobs=ready, attempt_index=3, batch_hash=batch_hash,
                    status="not_run", failure_code="shadow_not_run_breaker",
                ))
            break
        continue

    shadow_result = shadow_outcome.result
    if shadow_result.model_mismatch:
        shadow_store_fn(sb, failure_rows(
            jobs=ready, attempt_index=attempt_index, batch_hash=batch_hash,
            status="integrity_failure", failure_code="shadow_model_mismatch",
            model_actual=shadow_result.model_actual,
        ))
        if attempt_index == 2:
            shadow_store_fn(sb, failure_rows(
                jobs=ready, attempt_index=3, batch_hash=batch_hash,
                status="not_run", failure_code="shadow_not_run_breaker",
            ))
        breaker = "model"
        break

    shadow_store_fn(sb, success_rows(
        jobs=ready, attempt_index=attempt_index, batch_hash=batch_hash,
        model_actual=shadow_result.model_actual,
        provider_request_id=shadow_result.provider_request_id,
        results=shadow_result.results, usage=shadow_result.usage,
        provider_estimated_cost_usd=shadow_result.provider_estimated_cost_usd,
    ))
```

`ready`의 각 job에 provenance 필드가 없으면 shadow를 호출하기 전에
`ShadowIntegrityError`로 fail-closed한다. 기존 primary update dict, `counts`, Slack summary와
`clip_vlm_jobs.result`는 수정하지 않는다.

- [ ] **Step 4: shadow worker GREEN과 기존 worker 회귀를 확인한다**

Run:

```bash
uv run pytest -q tests/test_vlm_consensus_shadow_worker.py
uv run pytest -q tests/test_vlm_worker.py tests/test_vlm_runtime.py
```

Expected: PASS. 기존 테스트의 analyzer call count는 feature flag 기본 false라 변하지 않는다.

- [ ] **Step 5: Task 4를 커밋한다**

```bash
git add reporter/vlm_candidate_worker.py tests/test_vlm_consensus_shadow_worker.py
git commit -m "feat: 위험 라벨 batch shadow 3회 판독 연결"
```

### Task 5: 적대 회귀·운영 경계 검증

**Files:**
- Modify: `tests/test_vlm_consensus_shadow_worker.py`
- Modify: `tests/test_vlm_worker.py`

**Interfaces:**
- Consumes: Task 4 orchestration.
- Produces: breaker·partial failure·multi-camera·production mutation 0 회귀.

- [ ] **Step 1: 적대 테스트를 추가한다**

```python
from reporter.claude_cli_analyzer import CliBatchError


def test_attempt2_auth_breaker_records_attempt3_not_run_and_stops_next_camera():
    sb, jobs = sb_and_jobs()
    jobs[0].update(camera_id="camA", selector_run_id="runA")
    jobs[1].update(camera_id="camB", selector_run_id="runB")
    calls, writes = [], []

    def analyzer(frame_sets, model):
        calls.append(tuple(frame_sets))
        if len(calls) == 1:
            return result(frame_sets, "drinking", "request-1")
        raise CliBatchError("not_logged_in", disposition="breaker")

    output = process_cli_jobs(
        sb, jobs, analyzer=analyzer, auth_check=lambda: None,
        download_fn=lambda key, dest: dest,
        extract_fn=lambda video, out: [out / f"{i}.jpg" for i in range(6)],
        shadow_enabled=True,
        shadow_store_fn=lambda _sb, rows: writes.append(rows) or len(rows),
    )
    assert calls == [("c0",), ("c0",)]
    assert output.breaker == "auth"
    assert [batch[0]["status"] for batch in writes] == [
        "succeeded", "failed", "not_run"
    ]
    assert sb.store["clip_vlm_jobs"][0]["status"] == "succeeded"
    assert sb.store["clip_vlm_jobs"][1]["status"] == "queued"


def test_attempt2_provider_failure_still_allows_attempt3_when_not_breaker():
    sb, jobs = sb_and_jobs()
    calls, writes = [], []

    def analyzer(frame_sets, model):
        calls.append(tuple(frame_sets))
        if len(calls) == 1:
            return result(frame_sets, "drinking", "request-1")
        if len(calls) == 2:
            raise CliBatchError("provider_error: timeout", disposition="no_retry")
        return result(frame_sets, "moving", "request-3")

    output = process_cli_jobs(
        sb, jobs, analyzer=analyzer, auth_check=lambda: None,
        download_fn=lambda key, dest: dest,
        extract_fn=lambda video, out: [out / f"{i}.jpg" for i in range(6)],
        shadow_enabled=True,
        shadow_store_fn=lambda _sb, rows: writes.append(rows) or len(rows),
    )
    assert len(calls) == 3 and output.breaker is None
    assert [batch[0]["status"] for batch in writes] == [
        "succeeded", "failed", "succeeded"
    ]


def test_shadow_model_mismatch_never_overwrites_primary_model_or_action():
    sb, jobs = sb_and_jobs()
    calls, writes = [], []

    def analyzer(frame_sets, model):
        calls.append(tuple(frame_sets))
        exact = result(frame_sets, "drinking", "request-1")
        if len(calls) == 1:
            return exact
        return CliBatchResult(
            "request-2", model, "claude-sonnet-4-6", exact.results,
            exact.usage, exact.provider_estimated_cost_usd, True,
        )

    output = process_cli_jobs(
        sb, jobs, analyzer=analyzer, auth_check=lambda: None,
        download_fn=lambda key, dest: dest,
        extract_fn=lambda video, out: [out / f"{i}.jpg" for i in range(6)],
        shadow_enabled=True,
        shadow_store_fn=lambda _sb, rows: writes.append(rows) or len(rows),
    )
    assert output.breaker == "model"
    assert [batch[0]["status"] for batch in writes] == [
        "succeeded", "integrity_failure", "not_run"
    ]
    assert all(row["result"]["action"] == "drinking" for row in sb.store["clip_vlm_jobs"])
    assert all(row["model_actual"] == "claude-sonnet-5" for row in sb.store["clip_vlm_jobs"])


def test_shadow_never_calls_download_or_extract_more_than_once_per_clip():
    _, _, calls, downloads, extracts, writes = run_case(
        ["drinking", "drinking", "drinking"], enabled=True
    )
    assert len(calls) == 3 and len(writes) == 3
    assert downloads == ["c0.mp4", "c1.mp4"]
    assert len(extracts) == 2


def test_run_passes_next_schedule_as_default_cli_deadline(monkeypatch):
    from zoneinfo import ZoneInfo
    from reporter import config, vlm_candidate_worker as worker

    seen = []

    def fake_process(_sb, _jobs, *, deadline=None):
        seen.append(deadline)
        return {}

    monkeypatch.setattr(config, "VLM_PROVIDER", "claude_cli_batch")
    monkeypatch.setattr(worker, "process_cli_jobs", fake_process)
    now = datetime(2026, 7, 27, 22, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    rc = worker.run(
        sb=FakeSB(), now=now, enabled=True,
        expected_host="mac-mini", hostname_fn=lambda: "mac-mini",
        acquire_lock_fn=lambda: object(), release_lock_fn=lambda lock: None,
        load_current_fn=lambda *args, **kwargs: [],
        load_recovery_fn=lambda *args, **kwargs: [],
        send_fn=lambda text: True,
    )
    assert rc == 0
    assert seen
    assert all(
        deadline == datetime(2026, 7, 28, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        for deadline in seen
    )
```

- [ ] **Step 2: 테스트가 기존 구현의 빠진 경계를 잡는지 확인한다**

Run: `uv run pytest -q tests/test_vlm_consensus_shadow_worker.py`

Expected: 네 적대 시나리오가 모두 PASS. 실패하면 해당 breaker·model·download 경계의
최소 orchestration만 수정한다.

- [ ] **Step 3: production mutation fingerprint 테스트를 추가한다**

```python
def test_shadow_only_changes_shadow_ledger_and_primary_expected_fields():
    sb, _, calls, downloads, extracts, writes = run_case(
        ["drinking", "drinking", "drinking"], enabled=True
    )
    assert len(calls) == 3 and len(downloads) == 2 and len(extracts) == 2
    assert len(writes) == 3
    assert set(sb.store) == {"clip_vlm_jobs", "motion_clips"}
    assert all("consensus" not in row and "majority" not in row for row in sb.store["clip_vlm_jobs"])
```

- [ ] **Step 4: 관련 전체 회귀를 실행한다**

Run:

```bash
uv run pytest -q tests/test_vlm_consensus_shadow.py \
  tests/test_vlm_shadow_store.py \
  tests/test_vlm_consensus_shadow_worker.py \
  tests/test_vlm_worker.py \
  tests/test_vlm_runtime.py \
  tests/test_install_vlm_launchd.py
```

Expected: PASS.

- [ ] **Step 5: Task 5를 커밋한다**

```bash
git add tests/test_vlm_consensus_shadow_worker.py tests/test_vlm_worker.py \
  reporter/vlm_candidate_worker.py
git commit -m "test: VLM shadow 운영 경계 고정"
```

### Task 6: 전체 검증과 runtime handoff 보고

**Files:**
- Modify: `specs/next-session.md`
- Modify: `.claude/donts-audit.md`
- Create: `docs/handoff-prompts/2026-07-27-vlm-risk-consensus-shadow-worker-report.md`

**Interfaces:**
- Consumes: Tasks 1~5와 lab RPC contract.
- Produces: migration 적용 후 별도 배포 handoff가 사용할 구현 SHA.

- [ ] **Step 1: 전체 검증을 실행한다**

Run:

```bash
uv run pytest -q
uv run python -m compileall -q reporter
bash -n install-launchd-vlm-candidate.sh
git diff --check
```

Expected: 454 baseline을 포함한 전체 PASS, compile/shell/diff exit 0.

- [ ] **Step 2: 금지동작 정적 감사를 실행한다**

Run:

```bash
rg -n "reasoning|r2_key|signed_url|frame_path|email|majority|auto_moving|auto_skip" \
  reporter/vlm_consensus_shadow.py reporter/vlm_shadow_store.py
git diff origin/main -- reporter tests install-launchd-vlm-candidate.sh
```

Expected: 첫 명령에서 payload·log 저장 실행문 0. 두 번째 diff에 prompt, selector,
`anthropic_analyzer.py`, R2 삭제/write, Slack formatter, app/GT/behavior 코드 변경 0.

- [ ] **Step 3: 구현 보고서를 작성한다**

```markdown
# VLM risk consensus shadow worker implementation report

## Verdict
VLM_RISK_CONSENSUS_SHADOW_IMPLEMENTED_UNVERIFIED_RUNTIME

## Runtime defaults
- VLM_CONSENSUS_SHADOW_ENABLED=0
- protocol=risk-consensus-shadow-v1
- provider/model=claude_cli_batch/claude-sonnet-5
- shadow budget=1260s

## Evidence
- full pytest 명령과 실제 stdout을 이 절에 그대로 기록
- compileall/bash/diff: PASS
- feature-disabled provider calls: unchanged
- risk batch: same frame map, 3 calls, download/extract 1
- forbidden production mutations: 0

## Non-actions
lab migration apply, main merge, LaunchAgent install, Mac mini inference, R2/GT/app/behavior write = 0
```

- [ ] **Step 4: SOT 문서를 additive로 갱신한다**

`specs/next-session.md`에는 feature branch SHA, flag 기본 false, lab RPC dependency,
Mac mini 미실행을 기록한다. `.claude/donts-audit.md`에는 “shadow failure가 primary 결과를
되돌리거나 다수결로 덮어쓰면 안 된다”를 한 줄 추가한다.

- [ ] **Step 5: 문서와 보고서를 커밋하고 push한다**

```bash
git add specs/next-session.md .claude/donts-audit.md \
  docs/handoff-prompts/2026-07-27-vlm-risk-consensus-shadow-worker-report.md
git commit -m "docs: VLM consensus shadow 구현 증거 기록"
git push -u origin codex/vlm-risk-consensus-shadow-worker
```

Expected: local HEAD equals upstream, tracked tree clean. Main merge와 Mac mini 실행은 하지 않는다.
