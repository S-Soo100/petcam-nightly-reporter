import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reporter.claude_cli_analyzer import CliBatchError, analyze_batch


def _frames(tmp_path, clip_ids=("c1", "c2")):
    out = {}
    for clip_id in clip_ids:
        folder = tmp_path / clip_id
        folder.mkdir()
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
