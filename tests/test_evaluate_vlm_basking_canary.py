import json
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
