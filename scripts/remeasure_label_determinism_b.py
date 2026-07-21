"""P1 오탐 재측정 · 플랜 B — 구독 CLI(claude -p) 3회-일치 약식 프로토콜.

TEST-SHEET: experiments/label-determinism-remeasure/TEST-SHEET-B.md (pre-reg, 사후 변경 금지).
A안(remeasure_label_determinism.py, Messages API temp=0)의 약식 대체가 아님 — A안은 크레딧
결제 해소 후 확정판으로 유효. B는 temperature 비제어라 "3/3 일치 = 결정론" 보증 없음.

독립 배치 전용 — production 배선/launchd/plist/env 무변경, DB 접근 0 (sample_list.json만 읽음),
결과는 experiments/label-determinism-remeasure/results_b.json 파일로만 (클립·런 단위 durable → resume).

한도 주의: 구독 쿼터를 owner 본인·워커와 공유(claude-subscription-quota-shared). claude -p 는
한도/인증 실패도 rc 0 + is_error 봉투로 온다(claude-headless-silent-quota-failure) — 봉투 검사
필수, 감지 시 즉시 중단하고 진행분을 저장한다.

실행: uv run python scripts/remeasure_label_determinism_b.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 레포 루트 → reporter import (스크립트 직접 실행 컨벤션)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXP_DIR = REPO_ROOT / "experiments/label-determinism-remeasure"
SAMPLE_LIST_PATH = EXP_DIR / "sample_list.json"  # A안 동결 표본 그대로 (read-only)
RESULTS_PATH = EXP_DIR / "results_b.json"
V40_PROMPT_PATH = REPO_ROOT / "reporter/prompts/system.v4.0.md"  # v4.1 사용 금지 (canary REJECTED)

MODEL = "claude-sonnet-5"  # exact ID 계약 — alias 금지
RUNS_PER_CLIP = 3
CALL_GAP_SEC = 1.5
MAX_SUBATTEMPTS = 2  # 클립 단위 일시 실패(timeout/봉투 파싱)만. auth/quota/model 은 즉시 전체 중단
CALL_TIMEOUT_SEC = 300

# A안 V40_OUTPUT_SCHEMA 동일 (7-class, basking 없음)
SCHEMA_7CLASS = {
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
_ACTIONS = set(SCHEMA_7CLASS["properties"]["action"]["enum"])

# 안전 마커만 검사 — 원문은 저장하지 않는다 (claude_cli_analyzer redaction 원칙과 동일 결)
_LIMIT_MARKERS = ("session limit", "usage limit", "rate limit", "quota", "limit reached")


class QuotaAbort(RuntimeError):
    """한도/인증 실패 — 즉시 전체 중단. 진행분은 results_b.json 에 보존(resume 가능)."""


class FatalCliError(RuntimeError):
    """model mismatch 등 계약 위반 — 즉시 전체 중단."""


class CliCallError(RuntimeError):
    """일시 실패(timeout/봉투 파싱/스키마) — 호출 단위 subretry 대상."""


# --- 판정 (TEST-SHEET-B §5, 순수 함수) ---

def classify_outcome_b(fp_label, labels):
    """3/3 원 오탐 라벨 재현만 '진짜 오탐(강)'. 그 외(1~2회/0회)는 비결정성 귀속(약)."""
    if len(labels) == RUNS_PER_CLIP and all(lb == fp_label for lb in labels):
        return "true_fp_strong"
    return "nondeterminism_weak"


def decide_b(strong, total):
    """A안 §5 게이트 동일: ≤25% adopt / ≤50% hold / >50% reject (결론 라벨엔 '약식(B)' 표기)."""
    rate = strong / total
    if rate <= 0.25:
        return "adopt"
    if rate <= 0.50:
        return "hold"
    return "reject"


def detect_limit_code(text):
    """claude 원문에서 안전한 고정 코드만 뽑는다 (원문 저장 금지)."""
    lowered = text.lower()
    if "not logged in" in lowered:
        return "not_logged_in"
    if any(marker in lowered for marker in _LIMIT_MARKERS):
        return "quota_exceeded"
    return None


# --- CLI 호출 (classify.py 프레임 전달 + claude_cli_analyzer 봉투 검사 패턴) ---

def build_command(user_prompt, frame_dir):
    """2.1.177 실측 지원 플래그만 사용 (--max-turns 미지원이라 제외)."""
    return [
        "claude", "-p", user_prompt,
        "--safe-mode",                    # CLAUDE.md 등 커스터마이즈 차단 — 프롬프트 오염 방지
        "--tools", "Read",
        "--allowed-tools", "Read",        # -p 비대화 모드 권한 프롬프트 정지 방지
        "--add-dir", str(frame_dir),
        "--model", MODEL,
        "--effort", "low",                # production claude_cli_batch(drinking FP 생산 경로) 동일 + 쿼터 절약
        "--no-session-persistence",
        "--append-system-prompt-file", str(V40_PROMPT_PATH),
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA_7CLASS, separators=(",", ":")),
    ]


def parse_envelope(stdout):
    """claude --output-format json 봉투 → run dict. is_error 는 rc 0 이어도 온다 — 필수 검사."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise CliCallError("invalid_envelope") from e
    if not isinstance(envelope, dict):
        raise CliCallError("invalid_envelope")
    if envelope.get("is_error"):
        result_text = str(envelope.get("result") or "")
        code = detect_limit_code(result_text)
        if code:
            raise QuotaAbort(code)
        raise CliCallError("claude_cli_error")
    usage_map = envelope.get("modelUsage") or {}
    if not usage_map:
        raise CliCallError("missing_model_usage")
    model_actual = MODEL if MODEL in usage_map else next(iter(usage_map))
    if model_actual != MODEL:
        raise FatalCliError(f"model mismatch: requested={MODEL} actual={model_actual}")
    result = envelope.get("structured_output")
    if not isinstance(result, dict):  # 구버전 CLI 폴백: result 텍스트의 json 블록 슬라이스 (classify._parse 방식)
        text = str(envelope.get("result") or "")
        start, end = text.find("{"), text.rfind("}")
        result = None
        if start >= 0 and end > start:
            try:
                result = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                result = None
    if not isinstance(result, dict) or result.get("action") not in _ACTIONS:
        raise CliCallError("vlm_schema")
    raw_usage = usage_map[model_actual]
    return {
        "label": result["action"],
        "confidence": result.get("confidence"),
        "reasoning": result.get("reasoning"),
        "session_id": str(envelope.get("session_id") or ""),
        "usage": {
            "input_tokens": int(raw_usage.get("inputTokens") or 0),
            "cache_creation_input_tokens": int(raw_usage.get("cacheCreationInputTokens") or 0),
            "cache_read_input_tokens": int(raw_usage.get("cacheReadInputTokens") or 0),
            "output_tokens": int(raw_usage.get("outputTokens") or 0),
        },
        "cost_usd_estimated": float(raw_usage.get("costUSD") or envelope.get("total_cost_usd") or 0),
    }


def analyze_once(paths, sample, *, runner=subprocess.run):
    """단일 클립 1회 판독. duration 은 sample_list DB 실측값을 프롬프트 텍스트에 주입 (A안 §3 동일)."""
    listed = " ".join(str(p) for p in paths)
    user = (
        f"다음은 게코 사육장 CCTV 프레임 {len(paths)}장(시간순), 클립 길이 {float(sample['duration_sec']):.3f}초야. "
        f"각 파일을 Read 도구로 열어서 보고, 게코의 대표 행동 1개를 분류해. "
        f"JSON schema만 출력해. 프레임: {listed}"
    )
    try:
        completed = runner(build_command(user, paths[0].parent), capture_output=True,
                           text=True, timeout=CALL_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as e:
        raise CliCallError("timeout") from e
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        raise FatalCliError(f"spawn failed: {type(e).__name__}") from e
    if completed.returncode != 0:
        blob = f"{completed.stdout}\n{completed.stderr}"
        code = detect_limit_code(blob)
        if code:
            raise QuotaAbort(code)
        raise CliCallError(f"cli_rc_{completed.returncode}")
    return parse_envelope(completed.stdout)


def call_with_subretry(fn, *, sleep=time.sleep, max_subattempts=MAX_SUBATTEMPTS):
    """CliCallError(일시)만 최대 2회. QuotaAbort/FatalCliError 는 즉시 전파."""
    for attempt in range(1, max_subattempts + 1):
        try:
            return fn()
        except CliCallError:
            if attempt == max_subattempts:
                raise
            sleep(2 * attempt)


# --- 배치 (deps 주입 — 테스트는 fake, 실행은 실물) ---

def _write_durable(out_path, payload):
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(out_path)


def run_batch_b(samples, *, analyze_fn, download_fn, extract_fn, out_path, tmp_dir,
                sleep=time.sleep, runs_per_clip=RUNS_PER_CLIP):
    """클립당 runs_per_clip 회. 런 단위 durable 저장 + partial resume (한도 중단 대비)."""
    tmp_dir = Path(tmp_dir)
    results = json.loads(out_path.read_text()) if out_path.exists() else {"clips": {}}
    for idx, s in enumerate(samples, 1):
        cid = s["clip_id"]
        entry = results["clips"].get(cid)
        if entry and len(entry["runs"]) == runs_per_clip and not entry.get("partial"):
            continue  # resume — 완료 클립은 재호출하지 않는다
        if entry is None:
            entry = {"set": s["set"], "fp_label": s["fp_label"], "gt": s.get("gt"),
                     "runs": [], "partial": True}
            results["clips"][cid] = entry
        mp4 = download_fn(s["r2_key"], tmp_dir / f"{cid}.mp4")
        paths = extract_fn(mp4, tmp_dir / cid)
        try:
            while len(entry["runs"]) < runs_per_clip:
                run = call_with_subretry(lambda: analyze_fn(paths, s), sleep=sleep)
                entry["runs"].append(run)
                _write_durable(out_path, results)  # 런 단위 durable — 중단 시점까지 보존
                sleep(CALL_GAP_SEC)
        except (QuotaAbort, FatalCliError, CliCallError):
            _write_durable(out_path, results)
            raise
        labels = [r["label"] for r in entry["runs"]]
        entry.pop("partial", None)
        entry["unanimous"] = len(set(labels)) == 1
        entry["outcome_b"] = classify_outcome_b(s["fp_label"], labels)
        _write_durable(out_path, results)
        mp4.unlink(missing_ok=True)  # 클립 미디어 즉시 정리 (storage 누적 방지)
        shutil.rmtree(tmp_dir / cid, ignore_errors=True)
        print(f"[{idx}/{len(samples)}] {cid[:8]} {s['set']}: {labels} -> {entry['outcome_b']}", flush=True)
        if idx % 20 == 0:
            done = [c for c in results["clips"].values() if not c.get("partial")]
            strong = sum(1 for c in done if c.get("outcome_b") == "true_fp_strong")
            print(f"--- progress: {len(done)} clips done, strong={strong} ---", flush=True)
    return results


def summarize_b(results):
    clips = {cid: c for cid, c in results["clips"].items() if not c.get("partial")}
    strong = sorted(cid for cid, c in clips.items() if c["outcome_b"] == "true_fp_strong")
    unanimous = sum(1 for c in clips.values() if c.get("unanimous"))
    tokens = {"input_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0, "output_tokens": 0}
    for c in results["clips"].values():
        for r in c["runs"]:
            for k in tokens:
                tokens[k] += r["usage"][k]
    return {
        "protocol": "B (subscription CLI, temperature 비제어, 3/3-일치 약식)",
        "n_clips": len(clips), "true_fp_strong": strong, "n_true_fp_strong": len(strong),
        "n_nondeterminism_weak": len(clips) - len(strong),
        "unanimous_rate": (unanimous / len(clips)) if clips else None,
        "decision": (decide_b(len(strong), len(clips)) + " (약식 B)") if clips else None,
        "tokens": tokens, "cost_usd": "0 (구독)",
    }


def main():
    for tool in ("claude", "ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"ABORT: {tool} not found", file=sys.stderr)
            return 2
    # 사전 auth probe — 배치 진입 전에 로그인 상태를 실측 (침묵 실패 방지 1차 게이트)
    probe = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=15)
    try:
        logged_in = json.loads(probe.stdout).get("loggedIn") is True
    except (json.JSONDecodeError, AttributeError):
        logged_in = False
    if probe.returncode != 0 or not logged_in:
        print("ABORT: claude auth status 가 loggedIn=true 가 아님", file=sys.stderr)
        return 2
    from reporter import config, r2  # config import = .env 로드 (R2 자격)
    from reporter.vlm_frames import extract_six
    if not (config.R2_ENDPOINT and config.R2_BUCKET):
        print("ABORT: R2 설정 누락 (.env)", file=sys.stderr)
        return 2

    samples = json.loads(SAMPLE_LIST_PATH.read_text())["samples"]
    assert len(samples) == 42, f"sample_list 42건 고정 계약 위반: {len(samples)}"

    with tempfile.TemporaryDirectory(prefix="remeasure-b-") as tmp:
        try:
            results = run_batch_b(samples, analyze_fn=analyze_once, download_fn=r2.download_clip,
                                  extract_fn=extract_six, out_path=RESULTS_PATH, tmp_dir=Path(tmp))
        except QuotaAbort as e:
            print(f"ABORT(quota/auth): {e} — 진행분은 {RESULTS_PATH} 보존. 한도 리셋 후 재실행하면 이어서 돈다.",
                  file=sys.stderr)
            return 1
        except (FatalCliError, CliCallError) as e:
            print(f"ABORT: {e} — 진행분은 {RESULTS_PATH} 보존 (재실행 시 resume)", file=sys.stderr)
            return 1
    print(json.dumps(summarize_b(results), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
