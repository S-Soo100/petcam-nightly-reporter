import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reporter.claude_cli_analyzer import (
    CliBatchError,
    analyze_batch,
    analyze_batch_with_retry,
    check_cli_auth,
)
from reporter.vlm_budget import Usage


def _frames(tmp_path, clip_ids=("c1", "c2")):
    out = {}
    for clip_id in clip_ids:
        folder = tmp_path / clip_id
        folder.mkdir(exist_ok=True)
        out[clip_id] = []
        for index in range(6):
            path = folder / f"{index}.jpg"
            path.write_bytes(b"jpeg")
            out[clip_id].append(path)
    return out


def _envelope(items, model="claude-sonnet-5", is_error=False):
    return {
        "is_error": is_error,
        "session_id": "session-1",
        "structured_output": {"items": items},
        "modelUsage": {
            model: {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadInputTokens": 30,
                "cacheCreationInputTokens": 40,
                "costUSD": 0.0123,
            }
        },
    }


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


def test_cli_batch_uses_exact_model_and_read_only_command(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        items = [
            {"clip_id": "c1", "action": "moving", "confidence": 0.8, "reasoning": "moves"},
            {"clip_id": "c2", "action": "drinking", "confidence": 0.7, "reasoning": "licks"},
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(_envelope(items)), stderr="")

    result = analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=runner)
    command, kwargs = calls[0]
    assert command[0:2] == ["claude", "-p"]
    assert command[command.index("--model") + 1] == "claude-sonnet-5"
    assert command[command.index("--tools") + 1] == "Read"
    assert command[command.index("--allowed-tools") + 1] == "Read"
    assert "--safe-mode" in command
    assert "--no-session-persistence" in command
    assert kwargs["timeout"] == 300
    assert result.model_actual == "claude-sonnet-5"
    assert set(result.results) == {"c1", "c2"}
    assert result.usage.input_tokens == 100


def test_cli_batch_rejects_auth_error_model_mismatch_and_clip_set(tmp_path):
    frames = _frames(tmp_path)

    def response(payload, returncode=0):
        return lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode, stdout=json.dumps(payload), stderr="denied"
        )

    with pytest.raises(CliBatchError, match="provider_error"):
        analyze_batch(frames, "claude-sonnet-5", runner=response(_envelope([], is_error=True)))

    logged_out = _envelope([], is_error=True)
    logged_out["result"] = "Not logged in · Please run /login"
    with pytest.raises(CliBatchError, match="not_logged_in") as caught:
        analyze_batch(frames, "claude-sonnet-5", runner=response(logged_out))
    assert caught.value.code == "not_logged_in"

    max_turns = _envelope([], is_error=True)
    max_turns.update({"subtype": "error_max_turns", "terminal_reason": "max_turns"})
    with pytest.raises(CliBatchError, match="max_turns_exceeded") as caught:
        analyze_batch(frames, "claude-sonnet-5", runner=response(max_turns))
    assert caught.value.code == "max_turns_exceeded"

    items = [{"clip_id": "c1", "action": "moving", "confidence": 0.8, "reasoning": "moves"}]
    mismatch = analyze_batch(frames, "claude-sonnet-5", runner=response(_envelope(items, model="claude-sonnet-4-6")))
    assert mismatch.model_mismatch is True
    assert mismatch.model_actual == "claude-sonnet-4-6"

    with pytest.raises(CliBatchError, match="clip_set_mismatch"):
        analyze_batch(frames, "claude-sonnet-5", runner=response(_envelope(items)))


def test_cli_batch_requires_one_to_four_clips_and_six_frames(tmp_path):
    with pytest.raises(ValueError, match="1..4"):
        analyze_batch({}, "claude-sonnet-5")
    five = _frames(tmp_path, ("a", "b", "c", "d", "e"))
    with pytest.raises(ValueError, match="1..4"):
        analyze_batch(five, "claude-sonnet-5")
    bad = {"a": five["a"][:5]}
    with pytest.raises(ValueError, match="six frames"):
        analyze_batch(bad, "claude-sonnet-5")


def test_cli_batch_normalizes_timeout_as_provider_error(tmp_path):
    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 300)

    with pytest.raises(CliBatchError, match="provider_error: timeout"):
        analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=timeout)


@pytest.mark.parametrize("message", [
    "Session limit reached",
    "Usage limit exceeded",
    "Rate limit reached",
    "Account quota exhausted",
])
def test_cli_batch_normalizes_subscription_limits_without_leaking_details(tmp_path, message):
    envelope = _envelope([], is_error=True)
    envelope["result"] = f"{message} for secret-account@example.com"

    def limited(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")

    with pytest.raises(CliBatchError, match="quota_exceeded") as caught:
        analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=limited)
    assert caught.value.code == "quota_exceeded"
    assert "example.com" not in str(caught.value)


def test_cli_auth_probe_requires_logged_in_without_exposing_account_data():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True}), stderr="")

    check_cli_auth(runner=runner)
    assert calls[0][0] == ["claude", "auth", "status"]
    assert calls[0][1]["timeout"] == 15

    def logged_out(*_args, **_kwargs):
        payload = {"loggedIn": False, "email": "must-not-be-copied@example.com"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with pytest.raises(CliBatchError, match="not_logged_in") as caught:
        check_cli_auth(runner=logged_out)
    assert caught.value.code == "not_logged_in"
    assert "example.com" not in str(caught.value)


# --- Task 5: safe diagnostic, phase classification, bounded subretry ---

_DIRTY = (
    "\x1b[31mERROR\x1b[0m Claude failed for secret-account@example.com "
    "token=sk-abcdefghijklmnopqrstuvwxyz012345 at /Users/alice/petcam/run.py "
    "session 3f8b2c1a-1111-2222-3333-444455556666 on 2026-07-16T02:00:11+09:00"
)


def _rc1_runner(stderr):
    def runner(command, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr=stderr)
    return runner


def _diag_of(tmp_path, stderr):
    with pytest.raises(CliBatchError) as caught:
        analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=_rc1_runner(stderr))
    return caught.value


def test_diagnostic_redacts_secrets_and_keeps_only_byte_counts(tmp_path):
    exc = _diag_of(tmp_path, _DIRTY)
    blob = json.dumps(exc.diagnostic.to_dict())
    for secret in ("example.com", "sk-abcdefghij", "/Users/alice", "3f8b2c1a", "2026-07-16T02"):
        assert secret not in blob
        assert secret not in str(exc)
    assert exc.diagnostic.stderr_bytes == len(_DIRTY.encode())
    assert exc.diagnostic.stdout_bytes == 0
    # allowlist 밖 marker(예: 임의 계정 문자열)는 저장하지 않는다
    assert all(marker in (
        "not logged in", "session limit", "usage limit", "rate limit", "quota",
        "timeout", "max_turns", "permission denied", "command not found",
        "invalid json", "schema", "connection", "network",
    ) for marker in exc.diagnostic.markers)


def test_fingerprint_stable_across_variable_uuid_path_timestamp(tmp_path):
    a = _diag_of(tmp_path, "failed at /Users/alice/a.py uuid 3f8b2c1a-1111-2222-3333-444455556666 ts 2026-07-16T02:00:00Z")
    b = _diag_of(tmp_path, "failed at /Users/bob/z.py uuid 99999999-0000-1111-2222-333344445555 ts 2026-07-17T09:30:00Z")
    assert a.diagnostic.fingerprint == b.diagnostic.fingerprint


def test_check_cli_auth_failure_carries_auth_breaker_diagnostic():
    def logged_out(*_a, **_k):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": False, "email": "x@y.com"}), stderr="")
    with pytest.raises(CliBatchError) as caught:
        check_cli_auth(runner=logged_out)
    assert caught.value.diagnostic.phase == "auth"
    assert caught.value.disposition == "breaker"


@pytest.mark.parametrize("stderr,expected_phase,expected_disposition,expected_code", [
    ("some transient blip", "process", "retryable", "cli_rc_1"),
    ("Session limit reached", "process", "breaker", "quota_exceeded"),
])
def test_rc1_phase_and_disposition_classification(tmp_path, stderr, expected_phase, expected_disposition, expected_code):
    exc = _diag_of(tmp_path, stderr)
    assert exc.diagnostic.phase == expected_phase
    assert exc.disposition == expected_disposition
    assert exc.code == expected_code


def test_spawn_failure_is_breaker(tmp_path):
    def missing(*_a, **_k):
        raise FileNotFoundError("claude")
    with pytest.raises(CliBatchError) as caught:
        analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=missing)
    assert caught.value.diagnostic.phase == "spawn"
    assert caught.value.disposition == "breaker"


def test_timeout_is_process_retryable(tmp_path):
    def timeout(command, **_k):
        raise subprocess.TimeoutExpired(command, 300)
    with pytest.raises(CliBatchError) as caught:
        analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=timeout)
    assert caught.value.diagnostic.phase == "process"
    assert caught.value.disposition == "retryable"


def test_envelope_and_schema_failures_are_no_retry(tmp_path):
    def bad_envelope(*_a, **_k):
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")
    with pytest.raises(CliBatchError) as caught:
        analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=bad_envelope)
    assert caught.value.diagnostic.phase == "envelope"
    assert caught.value.disposition == "no_retry"

    schema_env = _envelope([{"action": "moving", "confidence": 0.8, "reasoning": "no clip_id"}])
    def bad_schema(*_a, **_k):
        return SimpleNamespace(returncode=0, stdout=json.dumps(schema_env), stderr="")
    with pytest.raises(CliBatchError, match="vlm_schema") as caught:
        analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=bad_schema)
    assert caught.value.diagnostic.phase == "schema"
    assert caught.value.disposition == "no_retry"


def test_clip_set_mismatch_is_clip_set_breaker(tmp_path):
    items = [{"clip_id": "c1", "action": "moving", "confidence": 0.8, "reasoning": "moves"}]
    def one_only(*_a, **_k):
        return SimpleNamespace(returncode=0, stdout=json.dumps(_envelope(items)), stderr="")
    with pytest.raises(CliBatchError, match="clip_set_mismatch") as caught:
        analyze_batch(_frames(tmp_path), "claude-sonnet-5", runner=one_only)
    assert caught.value.diagnostic.phase == "clip_set"
    assert caught.value.disposition == "breaker"


def _ok_result(model="claude-sonnet-5"):
    return SimpleNamespace(model_actual=model, model_mismatch=False)


def test_retry_first_attempt_success_has_null_diagnostic():
    calls = []
    def analyzer(frame_sets, model):
        calls.append((dict(frame_sets), model)); return _ok_result()
    outcome = analyze_batch_with_retry({"c1": ["x"], "c2": ["y"]}, "claude-sonnet-5", analyzer=analyzer)
    assert len(calls) == 1
    assert outcome.subattempts == 1 and outcome.recovered is False and outcome.diagnostic is None
    assert outcome.result is not None


@pytest.mark.parametrize("code,message", [
    ("timeout", "provider_error: timeout"),
    ("cli_rc_1", "provider_error: cli_rc_1"),
])
def test_retry_transient_then_success_marks_recovered(code, message):
    calls = []
    def analyzer(frame_sets, model):
        calls.append(1)
        if len(calls) == 1:
            raise CliBatchError(message, disposition="retryable")
        return _ok_result()
    outcome = analyze_batch_with_retry({"c1": ["x"]}, "claude-sonnet-5", analyzer=analyzer)
    assert len(calls) == 2 and outcome.subattempts == 2 and outcome.recovered is True
    assert outcome.result is not None


def test_retry_two_transient_failures_stop_at_two_not_recovered():
    calls = []
    def analyzer(frame_sets, model):
        calls.append(1); raise CliBatchError("provider_error: timeout", disposition="retryable")
    outcome = analyze_batch_with_retry({"c1": ["x"]}, "claude-sonnet-5", analyzer=analyzer)
    assert len(calls) == 2 and outcome.subattempts == 2 and outcome.recovered is False
    assert outcome.result is None and outcome.error is not None


@pytest.mark.parametrize("disposition", ["breaker", "no_retry"])
def test_retry_does_not_retry_terminal_dispositions(disposition):
    calls = []
    def analyzer(frame_sets, model):
        calls.append(1); raise CliBatchError("provider_error: x", disposition=disposition)
    outcome = analyze_batch_with_retry({"c1": ["x"]}, "claude-sonnet-5", analyzer=analyzer)
    assert len(calls) == 1 and outcome.subattempts == 1 and outcome.result is None


def test_retry_reuses_same_frame_map_and_model_no_split():
    seen = []
    def analyzer(frame_sets, model):
        seen.append((dict(frame_sets), model))
        if len(seen) == 1:
            raise CliBatchError("provider_error: timeout", disposition="retryable")
        return _ok_result()
    frame_sets = {"c1": ["a"], "c2": ["b"]}
    analyze_batch_with_retry(frame_sets, "claude-sonnet-5", analyzer=analyzer)
    assert seen[0] == seen[1] == ({"c1": ["a"], "c2": ["b"]}, "claude-sonnet-5")
