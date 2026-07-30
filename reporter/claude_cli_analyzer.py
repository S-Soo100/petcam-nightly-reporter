"""Claude Code 구독으로 카메라별 후보를 한 번에 판독하는 제한된 CLI adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from reporter.vlm_budget import Usage

_DIAGNOSTIC_VERSION = 1
_MAX_SUBATTEMPTS = 2

# 원문(계정/경로/토큰 포함 가능)을 저장하지 않고 안정적 fingerprint 만 남기기 위한 redaction.
# 순서 중요: ANSI → timestamp → email → uuid → token → path.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TOKEN_RE = re.compile(r"(?i)(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{16,}|[A-Za-z0-9_-]{24,})")
# 단일 세그먼트 절대경로(/tmp)도 redact — _redact 출력이 fingerprint 외 용도로 재사용돼도 누출 없게.
_PATH_RE = re.compile(r"/[\w.\-]+(?:/[\w.\-]+)*")

# DB/Slack/log 에 남겨도 되는 안전한 marker 만 화이트리스트. 그 외 문자열은 저장하지 않는다.
_ALLOWLIST_MARKERS = (
    "not logged in", "session limit", "usage limit", "rate limit", "quota",
    "timeout", "max_turns", "permission denied", "command not found",
    "invalid json", "schema", "connection", "network",
)


def _redact(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = _TS_RE.sub("<ts>", text)
    text = _EMAIL_RE.sub("<email>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _TOKEN_RE.sub("<token>", text)
    text = _PATH_RE.sub("<path>", text)
    return " ".join(text.split())


def _fingerprint(text: str) -> str:
    return hashlib.sha256(_redact(text).encode()).hexdigest()[:16]


def _markers(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(marker for marker in _ALLOWLIST_MARKERS if marker in lowered)


@dataclass(frozen=True, slots=True)
class CliFailureDiagnostic:
    """DB/Slack 에 남길 수 있는 redacted 실패 진단(설계 §8.1). 원문은 담지 않는다."""
    version: int
    phase: str          # auth|spawn|process|envelope|schema|clip_set|model|unknown
    code: str
    exit_code: int | None
    fingerprint: str
    markers: tuple[str, ...]
    stdout_bytes: int
    stderr_bytes: int
    provider_subattempts: int = 1
    recovered: bool = False

    def to_dict(self) -> dict:
        return {
            "version": self.version, "phase": self.phase, "code": self.code,
            "exit_code": self.exit_code, "fingerprint": self.fingerprint,
            "markers": list(self.markers), "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes, "provider_subattempts": self.provider_subattempts,
            "recovered": self.recovered,
        }


def clip_failure_diagnostic(exc: Exception, *, phase: str = "process") -> CliFailureDiagnostic:
    """R2/frame 등 clip 단위 예외를 redacted diagnostic 으로. 원문(경로 포함 가능)은 저장 안 함."""
    return _build_diagnostic(type(exc).__name__, phase, stderr=str(exc))


def _build_diagnostic(code, phase, *, exit_code=None, stdout="", stderr="",
                      subattempts=1, recovered=False) -> CliFailureDiagnostic:
    blob = f"{stdout}\n{stderr}"
    return CliFailureDiagnostic(
        version=_DIAGNOSTIC_VERSION, phase=phase, code=code, exit_code=exit_code,
        fingerprint=_fingerprint(blob), markers=_markers(blob),
        stdout_bytes=len(stdout.encode()), stderr_bytes=len(stderr.encode()),
        provider_subattempts=subattempts, recovered=recovered,
    )

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


# code → (phase, disposition). disposition: retryable(즉시 subretry) / breaker(이후 호출 중단) /
# no_retry(해당 batch 만 terminal, 다른 camera batch 는 계속). 설계 §8.2.
_CODE_TABLE = {
    "auth_probe_failed": ("auth", "breaker"),
    "not_logged_in": ("auth", "breaker"),
    "quota_exceeded": ("process", "breaker"),
    "clip_set_mismatch": ("clip_set", "breaker"),
    "timeout": ("process", "retryable"),
    "invalid_envelope": ("envelope", "no_retry"),
    "claude_cli_error": ("envelope", "no_retry"),
    "missing_model_usage": ("envelope", "no_retry"),
    "max_turns_exceeded": ("process", "no_retry"),
    "vlm_schema": ("schema", "no_retry"),
}


class CliBatchError(RuntimeError):
    def __init__(self, message: str, *, diagnostic: CliFailureDiagnostic | None = None,
                 disposition: str | None = None):
        super().__init__(message)
        self.code = message.rsplit(": ", 1)[-1]
        self.diagnostic = diagnostic
        if disposition is not None:
            self.disposition = disposition
        elif diagnostic is not None:
            self.disposition = _CODE_TABLE.get(self.code, ("process", "retryable"))[1]
        else:
            self.disposition = _CODE_TABLE.get(self.code, ("unknown", "no_retry"))[1]


def _fail(message, *, exit_code=None, stdout="", stderr="") -> CliBatchError:
    """분류표(phase/disposition)와 redacted diagnostic 을 붙여 CliBatchError 를 만든다.

    분류 불가한 provider_error rc(예: cli_rc_1)는 process/retryable 로 두어 1회 subretry 한다.
    """
    code = message.rsplit(": ", 1)[-1]
    phase, disposition = _CODE_TABLE.get(code, ("process", "retryable"))
    diagnostic = _build_diagnostic(code, phase, exit_code=exit_code, stdout=stdout, stderr=stderr)
    return CliBatchError(message, diagnostic=diagnostic, disposition=disposition)


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


def _auth_fail(code, *, exit_code=None, stderr="") -> CliBatchError:
    diagnostic = _build_diagnostic(code, "auth", exit_code=exit_code, stderr=stderr)
    return CliBatchError(code, diagnostic=diagnostic, disposition="breaker")


def check_cli_auth(*, runner=subprocess.run) -> None:
    try:
        completed = runner(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError) as exc:
        raise _auth_fail("auth_probe_failed") from exc
    if completed.returncode != 0:
        raise _auth_fail("auth_probe_failed", exit_code=completed.returncode, stderr=completed.stderr or "")
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise _auth_fail("auth_probe_failed") from exc
    if status.get("loggedIn") is not True:
        # 원문(email 등)을 남기지 않는다 — status dict 를 diagnostic 에 넣지 않음.
        raise _auth_fail("not_logged_in")


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
        raise _fail("provider_error: timeout") from exc
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        # 실행 파일 부재/권한 = spawn 단계 breaker. 원문 대신 예외 타입만 code 로.
        raise CliBatchError(
            f"provider_error: {type(exc).__name__}",
            diagnostic=_build_diagnostic(type(exc).__name__, "spawn", stderr=str(exc)),
            disposition="breaker",
        ) from exc
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        if completed.returncode == 0:
            raise _fail("provider_error: invalid_envelope", stdout=completed.stdout or "") from exc
        envelope = None
    if isinstance(envelope, dict) and envelope.get("is_error"):
        result_text = str(envelope.get("result") or "")
        failure_code = _safe_failure_code(f"{result_text}\n{completed.stderr or ''}")
        if not failure_code and (
            envelope.get("subtype") == "error_max_turns"
            or envelope.get("terminal_reason") == "max_turns"
        ):
            failure_code = "max_turns_exceeded"
        raise _fail(
            failure_code or "provider_error: claude_cli_error",
            exit_code=completed.returncode or None,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
    if completed.returncode != 0:
        blob = f"{completed.stdout}\n{completed.stderr}"
        safe_code = _safe_failure_code(blob)
        if safe_code:
            raise _fail(safe_code, exit_code=completed.returncode, stdout=completed.stdout or "", stderr=completed.stderr or "")
        raise _fail(f"provider_error: cli_rc_{completed.returncode}", exit_code=completed.returncode,
                    stdout=completed.stdout or "", stderr=completed.stderr or "")
    usage_map = envelope.get("modelUsage") or {}
    if not usage_map:
        raise _fail("provider_error: missing_model_usage")
    model_actual = model if model in usage_map else next(iter(usage_map))
    raw_usage = usage_map[model_actual]
    usage = Usage(
        int(raw_usage.get("inputTokens") or 0),
        int(raw_usage.get("cacheCreationInputTokens") or 0),
        int(raw_usage.get("cacheReadInputTokens") or 0),
        int(raw_usage.get("outputTokens") or 0),
    )
    items = (envelope.get("structured_output") or {}).get("items") or []
    if any(not isinstance(item, dict) or "clip_id" not in item or "action" not in item for item in items):
        raise _fail("provider_error: vlm_schema")
    results = {item["clip_id"]: item for item in items}
    mismatch = model_actual != model
    if not mismatch and set(results) != set(frame_sets):
        raise CliBatchError("clip_set_mismatch",
                            diagnostic=_build_diagnostic("clip_set_mismatch", "clip_set"),
                            disposition="breaker")
    return CliBatchResult(
        str(envelope.get("session_id") or ""), model, model_actual, results, usage,
        float(raw_usage.get("costUSD") or envelope.get("total_cost_usd") or 0), mismatch,
    )


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """analyze_batch_with_retry 결과. durable attempt 당 provider subattempt 는 최대 2회."""
    result: CliBatchResult | None
    subattempts: int
    recovered: bool
    diagnostic: CliFailureDiagnostic | None
    error: CliBatchError | None


def analyze_batch_with_retry(frame_sets, model, *, analyzer=analyze_batch,
                             sleep=lambda _s: None) -> BatchOutcome:
    """같은 frame map·exact model 로 최대 2회 subattempt. retryable 만 즉시 재시도한다.

    batch 를 4→2→1 로 쪼개지 않고, subattempt 는 durable attempt_count 를 늘리지 않는다.
    성공 시 이전에 회복한 실패가 있으면 recovered=true diagnostic 을 남긴다.
    """
    last_error = None
    for attempt in range(1, _MAX_SUBATTEMPTS + 1):
        try:
            result = analyzer(frame_sets, model)
        except CliBatchError as exc:
            last_error = exc
            if exc.disposition == "retryable" and attempt < _MAX_SUBATTEMPTS:
                sleep(0)
                continue
            diagnostic = exc.diagnostic
            if diagnostic is not None:
                diagnostic = replace(diagnostic, provider_subattempts=attempt, recovered=False)
            return BatchOutcome(None, attempt, False, diagnostic, exc)
        recovered = attempt > 1
        diagnostic = None
        if recovered and last_error is not None and last_error.diagnostic is not None:
            diagnostic = replace(last_error.diagnostic, provider_subattempts=attempt, recovered=True)
        return BatchOutcome(result, attempt, recovered, diagnostic, None)
