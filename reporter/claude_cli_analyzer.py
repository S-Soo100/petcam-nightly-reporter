"""Claude Code 구독으로 카메라별 후보를 한 번에 판독하는 제한된 CLI adapter."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from reporter.vlm_budget import Usage

_SYSTEM_FILE = Path(__file__).parent / "prompts" / "system.v4.0.md"
_ACTIONS = [
    "eating_paste", "eating_prey", "drinking", "shedding", "moving", "unseen", "hand_feeding"
]
_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "clip_id": {"type": "string"},
                    "action": {"type": "string", "enum": _ACTIONS},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reasoning": {"type": "string", "maxLength": 300},
                },
                "required": ["clip_id", "action", "confidence", "reasoning"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


class CliBatchError(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = message.rsplit(": ", 1)[-1]


@dataclass(frozen=True, slots=True)
class CliBatchResult:
    provider_request_id: str
    model_requested: str
    model_actual: str
    results: dict[str, dict]
    usage: Usage
    provider_estimated_cost_usd: float
    model_mismatch: bool


def _safe_failure_code(text: str) -> str | None:
    """Claude 원문은 계정 정보를 포함할 수 있어 안전한 고정 코드만 반환해."""
    lowered = text.lower()
    if "not logged in" in lowered:
        return "not_logged_in"
    if any(marker in lowered for marker in ("session limit", "usage limit", "rate limit", "quota")):
        return "quota_exceeded"
    return None


def check_cli_auth(*, runner=subprocess.run) -> None:
    try:
        completed = runner(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError) as exc:
        raise CliBatchError("auth_probe_failed") from exc
    if completed.returncode != 0:
        raise CliBatchError("auth_probe_failed")
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CliBatchError("auth_probe_failed") from exc
    if status.get("loggedIn") is not True:
        raise CliBatchError("not_logged_in")


def _validate(frame_sets: dict[str, list[Path]], model: str) -> None:
    if not 1 <= len(frame_sets) <= 4:
        raise ValueError("CLI batch requires 1..4 clips")
    if model == "sonnet" or not model.startswith("claude-"):
        raise ValueError("exact model id required")
    if any(len(paths) != 6 for paths in frame_sets.values()):
        raise ValueError("each clip requires six frames")


def analyze_batch(frame_sets, model, *, runner=subprocess.run) -> CliBatchResult:
    _validate(frame_sets, model)
    all_paths = [Path(path) for paths in frame_sets.values() for path in paths]
    common_dir = os.path.commonpath([str(path.parent) for path in all_paths])
    listing = "\n".join(
        f"- clip_id={clip_id}: " + ", ".join(str(path) for path in paths)
        for clip_id, paths in frame_sets.items()
    )
    prompt = (
        "각 clip의 프레임 6장을 모두 Read로 열어 시간순으로 보고 대표 행동 하나를 분류해. "
        "clip_id는 입력 그대로 반환해. JSON schema만 출력해.\n" + listing
    )
    command = [
        "claude", "-p", prompt,
        "--safe-mode",
        "--tools", "Read",
        "--allowed-tools", "Read",
        "--add-dir", common_dir,
        "--model", model,
        "--effort", "low",
        "--max-turns", "3",
        "--no-session-persistence",
        "--system-prompt-file", str(_SYSTEM_FILE),
        "--output-format", "json",
        "--json-schema", json.dumps(_SCHEMA, separators=(",", ":")),
    ]
    try:
        completed = runner(command, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise CliBatchError("provider_error: timeout") from exc
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise CliBatchError(f"provider_error: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        safe_code = _safe_failure_code(f"{completed.stdout}\n{completed.stderr}")
        if safe_code:
            raise CliBatchError(safe_code)
        raise CliBatchError(f"provider_error: cli_rc_{completed.returncode}")
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CliBatchError("provider_error: invalid_envelope") from exc
    if envelope.get("is_error"):
        result_text = str(envelope.get("result") or "").lower()
        safe_code = _safe_failure_code(result_text)
        if safe_code:
            raise CliBatchError(safe_code)
        if envelope.get("subtype") == "error_max_turns" or envelope.get("terminal_reason") == "max_turns":
            raise CliBatchError("max_turns_exceeded")
        raise CliBatchError("provider_error: claude_cli_error")
    usage_map = envelope.get("modelUsage") or {}
    if not usage_map:
        raise CliBatchError("provider_error: missing_model_usage")
    model_actual = model if model in usage_map else next(iter(usage_map))
    raw_usage = usage_map[model_actual]
    usage = Usage(
        int(raw_usage.get("inputTokens") or 0),
        int(raw_usage.get("cacheCreationInputTokens") or 0),
        int(raw_usage.get("cacheReadInputTokens") or 0),
        int(raw_usage.get("outputTokens") or 0),
    )
    items = (envelope.get("structured_output") or {}).get("items") or []
    results = {item["clip_id"]: item for item in items}
    mismatch = model_actual != model
    if not mismatch and set(results) != set(frame_sets):
        raise CliBatchError("clip_set_mismatch")
    return CliBatchResult(
        str(envelope.get("session_id") or ""), model, model_actual, results, usage,
        float(raw_usage.get("costUSD") or envelope.get("total_cost_usd") or 0), mismatch,
    )
