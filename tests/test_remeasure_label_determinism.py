"""P1 오탐 재측정 배치(scripts/remeasure_label_determinism.py) 순수 함수 + run_batch 테스트.

실 API/R2/DB 호출 0 — 전부 주입 fake. (TEST-SHEET: experiments/label-determinism-remeasure/)
"""
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import reporter.anthropic_analyzer as aa
from scripts.remeasure_label_determinism import (
    V40_OUTPUT_SCHEMA,
    V40_PROMPT_PATH,
    CostAbort,
    NonRetryableError,
    call_with_retry,
    classify_outcome,
    decide,
    is_retryable,
    majority_label,
    pin_v40,
    run_batch,
)


# --- v4.0 핀 계약 ---

def test_v40_schema_is_seven_class_without_basking():
    classes = V40_OUTPUT_SCHEMA["properties"]["action"]["enum"]
    assert classes == ["eating_paste", "eating_prey", "drinking", "shedding", "moving", "unseen", "hand_feeding"]
    assert "basking" not in classes


def test_v40_prompt_path_is_v40_file():
    assert V40_PROMPT_PATH.name == "system.v4.0.md"
    assert V40_PROMPT_PATH.exists()


def test_pin_v40_overrides_module_globals_without_touching_files():
    orig_prompt, orig_schema = aa.SYSTEM_PROMPT, aa.OUTPUT_SCHEMA
    try:
        pin_v40()
        assert aa.SYSTEM_PROMPT == V40_PROMPT_PATH.read_text()
        assert aa.OUTPUT_SCHEMA is V40_OUTPUT_SCHEMA
    finally:
        aa.SYSTEM_PROMPT, aa.OUTPUT_SCHEMA = orig_prompt, orig_schema


# --- 클립별 판정 (TEST-SHEET §5) ---

def test_majority_label_unanimous():
    assert majority_label(["moving", "moving", "moving"]) == ("moving", True)


def test_majority_label_two_of_three():
    assert majority_label(["shedding", "moving", "moving"]) == ("moving", False)


def test_majority_label_three_way_tie_has_no_representative():
    assert majority_label(["shedding", "moving", "unseen"]) == (None, False)


def test_classify_outcome_true_fp_when_majority_matches_fp_label():
    assert classify_outcome("shedding", ["shedding", "shedding", "moving"]) == "true_fp"


def test_classify_outcome_nondeterminism_when_majority_differs():
    assert classify_outcome("shedding", ["moving", "moving", "moving"]) == "nondeterminism"


def test_classify_outcome_three_way_tie_is_nondeterminism():
    assert classify_outcome("drinking", ["drinking", "moving", "unseen"]) == "nondeterminism"


# --- 전체 decision (TEST-SHEET §5: ≤25% adopt / ≤50% hold / >50% reject) ---

@pytest.mark.parametrize("true_fp,total,expected", [
    (0, 42, "adopt"),
    (10, 42, "adopt"),   # 23.8%
    (11, 42, "hold"),    # 26.2%
    (21, 42, "hold"),    # 50.0%
    (22, 42, "reject"),  # 52.4%
])
def test_decide_boundaries(true_fp, total, expected):
    assert decide(true_fp, total) == expected


# --- 재시도 정책 (일시 에러만 backoff, 4xx 즉시 중단) ---

class _FakeApiError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


def test_is_retryable_transient_status():
    assert is_retryable(_FakeApiError(429)) is True
    assert is_retryable(_FakeApiError(500)) is True
    assert is_retryable(_FakeApiError(529)) is True


def test_is_retryable_client_error_and_generic_are_not():
    assert is_retryable(_FakeApiError(400)) is False
    assert is_retryable(_FakeApiError(401)) is False
    assert is_retryable(ValueError("boom")) is False


def test_is_retryable_connection_error():
    import httpx
    from anthropic import APIConnectionError
    exc = APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    assert is_retryable(exc) is True


def test_call_with_retry_recovers_from_transient_then_succeeds():
    calls, sleeps = [], []
    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise _FakeApiError(529)
        return "ok"
    assert call_with_retry(fn, sleep=sleeps.append) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2


def test_call_with_retry_gives_up_after_three_retries():
    def fn():
        raise _FakeApiError(500)
    with pytest.raises(_FakeApiError):
        call_with_retry(fn, sleep=lambda _s: None)


def test_call_with_retry_raises_nonretryable_immediately():
    calls = []
    def fn():
        calls.append(1)
        raise _FakeApiError(401)
    with pytest.raises(NonRetryableError):
        call_with_retry(fn, sleep=lambda _s: None)
    assert len(calls) == 1


# --- run_batch (주입 fake, durable 저장/resume/cost abort) ---

def _sample(cid, fp="shedding"):
    return {"clip_id": cid, "r2_key": f"k/{cid}.mp4", "duration_sec": 31.0,
            "set": f"{fp}_fp", "fp_label": fp, "gt": "moving"}


def _fake_result(label, cost="0.010000"):
    usage = SimpleNamespace(input_tokens=2700, cache_creation_input_tokens=0,
                            cache_read_input_tokens=4500, output_tokens=120)
    return SimpleNamespace(provider_request_id="req_x", model_requested="claude-sonnet-5",
                           model_actual="claude-sonnet-5", result={"action": label, "confidence": 0.9,
                           "reasoning": "r"}, usage=usage, cost_usd=Decimal(cost), model_mismatch=False)


def _fakes(tmp_path, label="moving"):
    downloads, analyzed = [], []
    def download_fn(key, dest):
        downloads.append(key); dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4"); return dest
    def extract_fn(mp4, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        return [out_dir / f"f_{i}.jpg" for i in range(6)]
    def analyze_fn(paths, sample):
        analyzed.append(sample["clip_id"]); return _fake_result(label)
    return download_fn, extract_fn, analyze_fn, downloads, analyzed


def test_run_batch_three_runs_per_clip_and_durable_results(tmp_path):
    out = tmp_path / "results.json"
    download_fn, extract_fn, analyze_fn, downloads, analyzed = _fakes(tmp_path)
    res = run_batch([_sample("c1"), _sample("c2", fp="drinking")], analyze_fn=analyze_fn,
                    download_fn=download_fn, extract_fn=extract_fn, out_path=out,
                    tmp_dir=tmp_path / "media")
    assert len(analyzed) == 6 and downloads == ["k/c1.mp4", "k/c2.mp4"]
    saved = json.loads(out.read_text())
    assert set(saved["clips"]) == {"c1", "c2"}
    c1 = saved["clips"]["c1"]
    assert [r["label"] for r in c1["runs"]] == ["moving"] * 3
    assert c1["outcome"] == "nondeterminism" and c1["unanimous"] is True
    assert res["clips"]["c2"]["outcome"] == "nondeterminism"


def test_run_batch_resume_skips_completed_clips(tmp_path):
    out = tmp_path / "results.json"
    download_fn, extract_fn, analyze_fn, downloads, analyzed = _fakes(tmp_path)
    prior = {"clips": {"c1": {"fp_label": "shedding", "runs": [{"label": "moving", "cost_usd": "0.01"}] * 3,
                              "outcome": "nondeterminism", "unanimous": True}}}
    out.write_text(json.dumps(prior))
    run_batch([_sample("c1"), _sample("c2")], analyze_fn=analyze_fn, download_fn=download_fn,
              extract_fn=extract_fn, out_path=out, tmp_dir=tmp_path / "media")
    assert analyzed == ["c2", "c2", "c2"]
    assert set(json.loads(out.read_text())["clips"]) == {"c1", "c2"}


def test_run_batch_cost_abort_persists_partial_results(tmp_path):
    out = tmp_path / "results.json"
    download_fn, extract_fn, _, _, analyzed = _fakes(tmp_path)
    def pricey_analyze(paths, sample):
        analyzed.append(sample["clip_id"]); return _fake_result("moving", cost="2.000000")
    with pytest.raises(CostAbort):
        run_batch([_sample("c1"), _sample("c2")], analyze_fn=pricey_analyze, download_fn=download_fn,
                  extract_fn=extract_fn, out_path=out, tmp_dir=tmp_path / "media",
                  max_cost_usd=Decimal("5.00"))
    # 3번째 호출에서 누적 $6 > $5 → 중단. 부분 결과는 파일로 남는다.
    assert len(analyzed) == 3
    assert out.exists()


def test_run_batch_aborts_on_model_mismatch(tmp_path):
    out = tmp_path / "results.json"
    download_fn, extract_fn, _, _, _ = _fakes(tmp_path)
    def mismatched(paths, sample):
        r = _fake_result("moving")
        return SimpleNamespace(**{**r.__dict__, "model_actual": "claude-other", "model_mismatch": True})
    with pytest.raises(NonRetryableError, match="model"):
        run_batch([_sample("c1")], analyze_fn=mismatched, download_fn=download_fn,
                  extract_fn=extract_fn, out_path=out, tmp_dir=tmp_path / "media")
