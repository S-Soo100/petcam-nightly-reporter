"""플랜 B 배치(scripts/remeasure_label_determinism_b.py) 순수 함수 + run_batch_b 테스트.

실 CLI/R2 호출 0 — 전부 주입 fake. (TEST-SHEET-B: experiments/label-determinism-remeasure/)
"""
import json

import pytest

from scripts.remeasure_label_determinism_b import (
    MODEL,
    SCHEMA_7CLASS,
    CliCallError,
    FatalCliError,
    QuotaAbort,
    build_command,
    call_with_subretry,
    classify_outcome_b,
    decide_b,
    detect_limit_code,
    parse_envelope,
    run_batch_b,
)


# --- 스키마/커맨드 계약 (TEST-SHEET-B §3) ---

def test_schema_is_seven_class_without_basking():
    classes = SCHEMA_7CLASS["properties"]["action"]["enum"]
    assert classes == ["eating_paste", "eating_prey", "drinking", "shedding", "moving", "unseen", "hand_feeding"]
    assert "basking" not in classes


def test_build_command_pins_v40_exact_model_and_json():
    cmd = build_command("user prompt", "/tmp/frames")
    assert cmd[:3] == ["claude", "-p", "user prompt"]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
    assert "sonnet" not in [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "--model"][1:]  # alias 없음
    appended = cmd[cmd.index("--append-system-prompt-file") + 1]
    assert appended.endswith("reporter/prompts/system.v4.0.md")  # v4.1 금지
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == SCHEMA_7CLASS
    assert "--safe-mode" in cmd and "--no-session-persistence" in cmd
    assert "--max-turns" not in cmd  # 2.1.177 미지원


# --- 클립별 판정 (TEST-SHEET-B §5: 3/3 재현만 강) ---

def test_outcome_strong_only_when_all_three_reproduce():
    assert classify_outcome_b("shedding", ["shedding", "shedding", "shedding"]) == "true_fp_strong"


@pytest.mark.parametrize("labels", [
    ["shedding", "shedding", "moving"],   # 2/3 — A안이면 true_fp, B는 약
    ["shedding", "moving", "moving"],
    ["moving", "moving", "moving"],
    ["drinking", "moving", "unseen"],
])
def test_outcome_weak_for_partial_or_zero_reproduction(labels):
    assert classify_outcome_b("shedding", labels) == "nondeterminism_weak"


# --- 전체 decision (A안 §5 게이트 동일, 라벨만 약식) ---

@pytest.mark.parametrize("strong,total,expected", [
    (0, 42, "adopt"),
    (10, 42, "adopt"),   # 23.8%
    (11, 42, "hold"),    # 26.2%
    (21, 42, "hold"),    # 50.0%
    (22, 42, "reject"),  # 52.4%
])
def test_decide_boundaries(strong, total, expected):
    assert decide_b(strong, total) == expected


# --- 한도/인증 감지 (claude-headless-silent-quota-failure 대응) ---

def test_detect_limit_code_matches_known_markers():
    assert detect_limit_code("Not logged in to Claude") == "not_logged_in"
    assert detect_limit_code("You have hit your session limit") == "quota_exceeded"
    assert detect_limit_code("usage limit reached · resets 1:10am") == "quota_exceeded"
    assert detect_limit_code("ordinary model text") is None


# --- envelope 파싱 ---

def _envelope(action="moving", model=MODEL, **over):
    env = {
        "session_id": "s1", "is_error": False,
        "result": "관찰 요약\n```json\n" + json.dumps({"action": action, "confidence": 0.9, "reasoning": "r"}) + "\n```",
        "structured_output": {"action": action, "confidence": 0.9, "reasoning": "r"},
        "modelUsage": {model: {"inputTokens": 100, "cacheCreationInputTokens": 5,
                               "cacheReadInputTokens": 50, "outputTokens": 20, "costUSD": 0.01}},
    }
    env.update(over)
    return env


def test_parse_envelope_prefers_structured_output():
    run = parse_envelope(json.dumps(_envelope("shedding")))
    assert run["label"] == "shedding" and run["confidence"] == 0.9
    assert run["usage"]["input_tokens"] == 100 and run["usage"]["cache_read_input_tokens"] == 50
    assert run["session_id"] == "s1"


def test_parse_envelope_falls_back_to_result_text_json():
    run = parse_envelope(json.dumps(_envelope("drinking", structured_output=None)))
    assert run["label"] == "drinking"


def test_parse_envelope_is_error_quota_raises_quota_abort():
    env = _envelope(is_error=True, result="5-hour limit reached · resets 1:10am")
    with pytest.raises(QuotaAbort):
        parse_envelope(json.dumps(env))


def test_parse_envelope_is_error_other_is_retryable_call_error():
    env = _envelope(is_error=True, result="something exploded")
    with pytest.raises(CliCallError):
        parse_envelope(json.dumps(env))


def test_parse_envelope_model_mismatch_is_fatal():
    with pytest.raises(FatalCliError, match="model"):
        parse_envelope(json.dumps(_envelope(model="claude-other-1")))


def test_parse_envelope_bad_action_is_call_error():
    with pytest.raises(CliCallError):
        parse_envelope(json.dumps(_envelope("basking")))


def test_parse_envelope_garbage_stdout_is_call_error():
    with pytest.raises(CliCallError):
        parse_envelope("not json at all")


# --- subretry: 일시 실패만 최대 2회, QuotaAbort/Fatal 즉시 전파 ---

def test_call_with_subretry_recovers_once():
    calls = []
    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise CliCallError("invalid_envelope")
        return {"label": "moving"}
    assert call_with_subretry(fn, sleep=lambda _s: None)["label"] == "moving"
    assert len(calls) == 2


def test_call_with_subretry_gives_up_after_max():
    def fn():
        raise CliCallError("timeout")
    with pytest.raises(CliCallError):
        call_with_subretry(fn, sleep=lambda _s: None)


def test_call_with_subretry_propagates_quota_immediately():
    calls = []
    def fn():
        calls.append(1)
        raise QuotaAbort("quota_exceeded")
    with pytest.raises(QuotaAbort):
        call_with_subretry(fn, sleep=lambda _s: None)
    assert len(calls) == 1


# --- run_batch_b (durable/resume/quota 중단) ---

def _sample(cid, fp="shedding"):
    return {"clip_id": cid, "r2_key": f"k/{cid}.mp4", "duration_sec": 31.0,
            "set": f"{fp}_fp", "fp_label": fp, "gt": "moving"}


def _run(label):
    return {"label": label, "confidence": 0.9, "reasoning": "r", "session_id": "s",
            "usage": {"input_tokens": 100, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 50, "output_tokens": 20},
            "cost_usd_estimated": 0.01}


def _fakes(labels_by_clip):
    downloads, analyzed = [], []
    def download_fn(key, dest):
        downloads.append(key); dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4"); return dest
    def extract_fn(mp4, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        return [out_dir / f"f_{i}.jpg" for i in range(6)]
    def analyze_fn(paths, sample):
        cid = sample["clip_id"]; analyzed.append(cid)
        seq = labels_by_clip[cid]
        return _run(seq[sum(1 for a in analyzed if a == cid) - 1])
    return download_fn, extract_fn, analyze_fn, downloads, analyzed


def test_run_batch_b_three_runs_strong_and_weak(tmp_path):
    out = tmp_path / "results_b.json"
    download_fn, extract_fn, analyze_fn, downloads, analyzed = _fakes({
        "c1": ["shedding", "shedding", "shedding"],   # 3/3 재현 → 강
        "c2": ["shedding", "moving", "shedding"],     # 2/3 → 약 (A안과 갈리는 케이스)
    })
    res = run_batch_b([_sample("c1"), _sample("c2")], analyze_fn=analyze_fn,
                      download_fn=download_fn, extract_fn=extract_fn, out_path=out,
                      tmp_dir=tmp_path / "media", sleep=lambda _s: None)
    assert len(analyzed) == 6 and downloads == ["k/c1.mp4", "k/c2.mp4"]
    saved = json.loads(out.read_text())
    assert saved["clips"]["c1"]["outcome_b"] == "true_fp_strong"
    assert saved["clips"]["c1"]["unanimous"] is True
    assert saved["clips"]["c2"]["outcome_b"] == "nondeterminism_weak"
    assert saved["clips"]["c2"]["unanimous"] is False
    assert res["clips"]["c2"]["runs"][1]["label"] == "moving"


def test_run_batch_b_resumes_partial_clip_without_rerunning_done_calls(tmp_path):
    out = tmp_path / "results_b.json"
    prior = {"clips": {"c1": {"set": "shedding_fp", "fp_label": "shedding", "gt": "moving",
                              "runs": [_run("shedding")], "partial": True}}}
    out.write_text(json.dumps(prior))
    download_fn, extract_fn, analyze_fn, _, analyzed = _fakes({"c1": ["shedding", "shedding", "shedding"]})
    run_batch_b([_sample("c1")], analyze_fn=analyze_fn, download_fn=download_fn,
                extract_fn=extract_fn, out_path=out, tmp_dir=tmp_path / "media",
                sleep=lambda _s: None)
    assert analyzed == ["c1", "c1"]  # 남은 2회만
    saved = json.loads(out.read_text())["clips"]["c1"]
    assert len(saved["runs"]) == 3 and "partial" not in saved
    assert saved["outcome_b"] == "true_fp_strong"


def test_run_batch_b_skips_completed_clips(tmp_path):
    out = tmp_path / "results_b.json"
    prior = {"clips": {"c1": {"set": "shedding_fp", "fp_label": "shedding", "gt": "moving",
                              "runs": [_run("moving")] * 3, "unanimous": True,
                              "outcome_b": "nondeterminism_weak"}}}
    out.write_text(json.dumps(prior))
    download_fn, extract_fn, analyze_fn, downloads, analyzed = _fakes({"c2": ["moving"] * 3})
    run_batch_b([_sample("c1"), _sample("c2")], analyze_fn=analyze_fn, download_fn=download_fn,
                extract_fn=extract_fn, out_path=out, tmp_dir=tmp_path / "media",
                sleep=lambda _s: None)
    assert analyzed == ["c2"] * 3 and downloads == ["k/c2.mp4"]


def test_run_batch_b_quota_abort_persists_progress_for_resume(tmp_path):
    out = tmp_path / "results_b.json"
    calls = []
    def analyze_fn(paths, sample):
        calls.append(sample["clip_id"])
        if len(calls) == 5:  # c2의 2번째 호출에서 한도
            raise QuotaAbort("quota_exceeded")
        return _run("moving")
    download_fn, extract_fn, _, _, _ = _fakes({})
    with pytest.raises(QuotaAbort):
        run_batch_b([_sample("c1"), _sample("c2")], analyze_fn=analyze_fn,
                    download_fn=download_fn, extract_fn=extract_fn, out_path=out,
                    tmp_dir=tmp_path / "media", sleep=lambda _s: None)
    saved = json.loads(out.read_text())["clips"]
    assert len(saved["c1"]["runs"]) == 3 and saved["c1"]["outcome_b"] == "nondeterminism_weak"
    assert len(saved["c2"]["runs"]) == 1 and saved["c2"]["partial"] is True
