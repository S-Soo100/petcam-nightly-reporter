"""P1 오탐 재측정 배치 — 결정론 경로(Messages API, temp=0, v4.0)로 기존 오탐 42건 재추론.

TEST-SHEET: experiments/label-determinism-remeasure/TEST-SHEET.md (pre-reg, 사후 변경 금지).
독립 배치 전용 — production 배선/launchd/plist/env 무변경, production 테이블 쓰기 0
(결과는 experiments/label-determinism-remeasure/results.json 파일로만).

v4.0 핀: 이 브랜치의 reporter/anthropic_analyzer.py 는 v4.1 프롬프트+8-class 스키마를
모듈 전역으로 로드한다. 파일은 수정하지 않고(금지 계약) 실행 시 모듈 전역만
production main 618f4f8 동치(v4.0 프롬프트 + 7-class 스키마)로 override 한다 — pin_v40().

실행: uv run python scripts/remeasure_label_determinism.py
전제: .env 에 ANTHROPIC_API_KEY (부재 시 exit 2 — 키 생성/요청 금지, owner 가 직접 세팅).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 레포 루트 → reporter import (스크립트 직접 실행 컨벤션)

import reporter.anthropic_analyzer as aa
from reporter import config, r2  # config import = .env 로드
from reporter.vlm_frames import extract_six

REPO_ROOT = Path(__file__).resolve().parent.parent
EXP_DIR = REPO_ROOT / "experiments/label-determinism-remeasure"
SAMPLE_LIST_PATH = EXP_DIR / "sample_list.json"
RESULTS_PATH = EXP_DIR / "results.json"

# v4.0 핀 대상 (production main 618f4f8 의 anthropic_analyzer 와 동치)
V40_PROMPT_PATH = REPO_ROOT / "reporter/prompts/system.v4.0.md"
V40_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["eating_paste", "eating_prey", "drinking",
                                              "shedding", "moving", "unseen", "hand_feeding"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string", "maxLength": 300},
    },
    "required": ["action", "confidence", "reasoning"],
    "additionalProperties": False,
}

MODEL = "claude-sonnet-5"  # exact ID 계약 — alias 금지
RUNS_PER_CLIP = 3
MAX_COST_USD = Decimal("5.00")  # TEST-SHEET §6 하드 게이트
MAX_SUBRETRIES = 3


class CostAbort(RuntimeError):
    """누적 실비용이 하드 게이트를 초과 — 부분 결과는 results.json 에 보존됨."""


class NonRetryableError(RuntimeError):
    """4xx 인증/요청 오류 또는 model mismatch — 즉시 전체 중단."""


def pin_v40():
    """anthropic_analyzer 모듈 전역을 v4.0 프롬프트 + 7-class 스키마로 override (파일 무수정)."""
    aa.SYSTEM_PROMPT = V40_PROMPT_PATH.read_text()
    aa.OUTPUT_SCHEMA = V40_OUTPUT_SCHEMA


# --- 판정 (TEST-SHEET §5, 순수 함수) ---

def majority_label(labels):
    """3회 라벨의 최빈(≥2회) 라벨. 3-way 불일치면 (None, False)."""
    top, n = Counter(labels).most_common(1)[0]
    if n < 2:
        return None, False
    return top, n == len(labels)


def classify_outcome(fp_label, labels):
    """대표 라벨이 원 오탐 라벨을 재현하면 true_fp, 아니면 nondeterminism 귀속."""
    rep, _ = majority_label(labels)
    return "true_fp" if rep == fp_label else "nondeterminism"


def decide(true_fp, total):
    """전체 decision: ≤25% adopt / ≤50% hold / >50% reject (사전 고정 게이트)."""
    rate = true_fp / total
    if rate <= 0.25:
        return "adopt"
    if rate <= 0.50:
        return "hold"
    return "reject"


# --- 재시도 (donts/vlm §4: 일시 에러만 backoff, 영구 실패 즉시 중단) ---

def is_retryable(exc):
    """429/5xx/connection 만 재시도. anthropic SDK 예외는 status_code 속성을 노출한다."""
    try:
        from anthropic import APIConnectionError
        if isinstance(exc, APIConnectionError):
            return True
    except ImportError:  # 테스트 환경 방어 (실환경엔 dependency 로 존재)
        pass
    sc = getattr(exc, "status_code", None)
    return sc is not None and (sc == 429 or sc >= 500)


def call_with_retry(fn, *, sleep=time.sleep, max_subretries=MAX_SUBRETRIES):
    for attempt in range(max_subretries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — 분기 후 즉시 re-raise/chain, 침묵 없음
            if not is_retryable(e):
                if getattr(e, "status_code", None) is not None:
                    raise NonRetryableError(f"non-retryable API error (status={e.status_code})") from e
                raise
            if attempt == max_subretries:
                raise
            sleep(2 ** attempt)


# --- 배치 실행 (deps 주입 — 테스트는 fake, 실행은 실물) ---

def _write_durable(out_path, payload):
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(out_path)


def run_batch(samples, *, analyze_fn, download_fn, extract_fn, out_path, tmp_dir,
              sleep=time.sleep, max_cost_usd=MAX_COST_USD, runs_per_clip=RUNS_PER_CLIP):
    """클립당 runs_per_clip 회 재추론. 클립 단위 durable 저장 + resume + cost 하드 게이트."""
    tmp_dir = Path(tmp_dir)
    results = json.loads(out_path.read_text()) if out_path.exists() else {"clips": {}}
    total_cost = sum(Decimal(r["cost_usd"]) for c in results["clips"].values() for r in c["runs"])
    for s in samples:
        cid = s["clip_id"]
        done = results["clips"].get(cid)
        if done and len(done["runs"]) == runs_per_clip:
            continue  # resume — 이미 완료한 클립은 재과금하지 않는다
        mp4 = download_fn(s["r2_key"], tmp_dir / f"{cid}.mp4")
        paths = extract_fn(mp4, tmp_dir / cid)
        runs = []
        for _ in range(runs_per_clip):
            res = call_with_retry(lambda: analyze_fn(paths, s), sleep=sleep)
            if res.model_mismatch:
                raise NonRetryableError(f"model mismatch: requested={res.model_requested} actual={res.model_actual}")
            total_cost += res.cost_usd
            runs.append({
                "label": res.result["action"], "confidence": res.result.get("confidence"),
                "reasoning": res.result.get("reasoning"), "provider_request_id": res.provider_request_id,
                "usage": {"input_tokens": res.usage.input_tokens,
                          "cache_creation_input_tokens": res.usage.cache_creation_input_tokens,
                          "cache_read_input_tokens": res.usage.cache_read_input_tokens,
                          "output_tokens": res.usage.output_tokens},
                "cost_usd": str(res.cost_usd),
            })
            if total_cost > max_cost_usd:
                labels = [r["label"] for r in runs]
                rep, unanimous = majority_label(labels)
                results["clips"][cid] = {"set": s["set"], "fp_label": s["fp_label"], "gt": s.get("gt"),
                                         "runs": runs, "partial": True, "representative": rep,
                                         "unanimous": unanimous, "outcome": None}
                _write_durable(out_path, results)
                raise CostAbort(f"accumulated cost ${total_cost} exceeds ${max_cost_usd}")
        labels = [r["label"] for r in runs]
        rep, unanimous = majority_label(labels)
        results["clips"][cid] = {"set": s["set"], "fp_label": s["fp_label"], "gt": s.get("gt"),
                                 "runs": runs, "representative": rep, "unanimous": unanimous,
                                 "outcome": classify_outcome(s["fp_label"], labels)}
        _write_durable(out_path, results)
        # 클립 미디어는 즉시 정리 (storage 누적 방지)
        mp4.unlink(missing_ok=True)
        shutil.rmtree(tmp_dir / cid, ignore_errors=True)
    return results


def summarize(results):
    clips = {cid: c for cid, c in results["clips"].items() if not c.get("partial")}
    true_fp = [cid for cid, c in clips.items() if c["outcome"] == "true_fp"]
    unanimous = sum(1 for c in clips.values() if c["unanimous"])
    total_cost = sum(Decimal(r["cost_usd"]) for c in results["clips"].values() for r in c["runs"])
    return {
        "n_clips": len(clips), "true_fp": sorted(true_fp), "n_true_fp": len(true_fp),
        "n_nondeterminism": len(clips) - len(true_fp),
        "unanimous_rate": (unanimous / len(clips)) if clips else None,
        "decision": decide(len(true_fp), len(clips)) if clips else None,
        "total_cost_usd": str(total_cost),
    }


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ABORT: ANTHROPIC_API_KEY 가 .env 에 없다. 키 세팅 전 실행 금지 (TEST-SHEET §7).", file=sys.stderr)
        return 2
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"ABORT: {tool} not found", file=sys.stderr)
            return 2
    if not (config.R2_ENDPOINT and config.R2_BUCKET):
        print("ABORT: R2 설정 누락 (.env)", file=sys.stderr)
        return 2

    samples = json.loads(SAMPLE_LIST_PATH.read_text())["samples"]
    assert len(samples) == 42, f"sample_list 42건 고정 계약 위반: {len(samples)}"

    pin_v40()
    from anthropic import Anthropic

    class _Clip:  # analyze_clip 은 id/duration_sec 만 사용 (production 은 DB duration 주입 — 동일)
        __slots__ = ("id", "duration_sec")
        def __init__(self, cid, dur):
            self.id, self.duration_sec = cid, dur

    client = Anthropic()
    analyze_fn = lambda paths, s: aa.analyze_clip(client, paths, _Clip(s["clip_id"], float(s["duration_sec"])), MODEL)  # noqa: E731

    with tempfile.TemporaryDirectory(prefix="remeasure-") as tmp:
        try:
            results = run_batch(samples, analyze_fn=analyze_fn, download_fn=r2.download_clip,
                                extract_fn=extract_six, out_path=RESULTS_PATH, tmp_dir=Path(tmp))
        except (CostAbort, NonRetryableError) as e:
            print(f"ABORT: {e} — 부분 결과는 {RESULTS_PATH} 보존", file=sys.stderr)
            return 1
    print(json.dumps(summarize(results), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
