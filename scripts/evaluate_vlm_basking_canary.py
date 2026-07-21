"""휴식(basking) 분류 blind canary — Mac mini 전용 진단 실행기.

host guard → shared VLM lock → manifest/video 검증 → auth → frame 추출 → 4/4/3 분석 순서로,
11개 local mp4만 읽어 사람 blind 판정과 대조한다. production DB/Slack/update_job/selector 를
import·호출하지 않으며, output JSON 에 reasoning·전체 UUID·경로를 남기지 않는다(진단 전용).
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path (스크립트 직접 실행)

from reporter.claude_cli_analyzer import analyze_batch_with_retry, check_cli_auth
from reporter.vlm_frames import extract_six
from reporter.vlm_host_guard import require_expected_host

_CLIP8_RE = re.compile(r"^[0-9a-f]{8}$")
_ALLOWED_ACTIONS = {"unseen", "moving", "basking"}
_BATCH_SIZE = 4
_EXPECTED_HOST = "baeg-endeuui-Macmini.local"


@dataclass(frozen=True, slots=True)
class CanaryCase:
    clip8: str
    filename: str
    expected_action: str
    product_outcome: str | None


@dataclass(frozen=True, slots=True)
class CanarySummary:
    total: int
    infra_failed: int
    exact_by_action: dict[str, int]
    visible_basking_as_unseen: int
    rows: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "infra_failed": self.infra_failed,
            "exact_by_action": self.exact_by_action,
            "visible_basking_as_unseen": self.visible_basking_as_unseen,
            "accepted": accepted(self),
            "rows": self.rows,
        }


def accepted(summary: CanarySummary) -> bool:
    exact = summary.exact_by_action
    return (
        summary.total == 11
        and summary.infra_failed == 0
        and exact.get("unseen", 0) == 3
        and exact.get("moving", 0) == 3
        and exact.get("basking", 0) >= 4
        and summary.visible_basking_as_unseen == 0
    )


def load_manifest(path: Path) -> Sequence[CanaryCase]:
    data = json.loads(Path(path).read_text())
    cases = data.get("cases", [])
    if len(cases) != 11:
        raise ValueError(f"manifest must have exactly 11 cases, got {len(cases)}")
    out: list[CanaryCase] = []
    seen: set[str] = set()
    for case in cases:
        clip8 = str(case["clip8"])
        filename = str(case["filename"])
        action = str(case["expected_action"])
        if not _CLIP8_RE.match(clip8):
            raise ValueError(f"invalid clip8: {clip8}")
        if action not in _ALLOWED_ACTIONS:
            raise ValueError(f"invalid expected_action: {action}")
        if filename != Path(filename).name:
            raise ValueError(f"filename must be a basename: {filename}")
        if clip8 in seen:
            raise ValueError(f"duplicate clip8: {clip8}")
        seen.add(clip8)
        out.append(CanaryCase(clip8, filename, action, case.get("product_outcome")))
    return tuple(out)


def _verify_media(cases: Sequence[CanaryCase], video_dir: Path) -> None:
    """manifest filename 과 video directory 의 mp4 가 1:1 인지 검증(Claude 호출 전)."""
    manifest_files = {case.filename for case in cases}
    present = {p.name for p in Path(video_dir).glob("*.mp4")}
    missing = manifest_files - present
    extra = present - manifest_files
    if missing or extra:
        raise ValueError(f"video mismatch missing={sorted(missing)} extra={sorted(extra)}")


def evaluate_cases(cases, video_dir, model, *, analyzer, extract_fn) -> CanarySummary:
    """4/4/3 batch 로 진단. analyzer(frame_sets, model) 는 BatchOutcome 을 반환한다."""
    cases = tuple(cases)
    rows: list[dict[str, object]] = []
    infra_failed = 0
    exact: dict[str, int] = {}
    visible_basking_as_unseen = 0
    with tempfile.TemporaryDirectory() as tmp:
        for start in range(0, len(cases), _BATCH_SIZE):
            batch = cases[start:start + _BATCH_SIZE]
            frame_sets = {}
            for case in batch:
                out_dir = Path(tmp) / case.clip8
                frame_sets[case.clip8] = extract_fn(str(Path(video_dir) / case.filename), out_dir)
            outcome = analyzer(frame_sets, model)
            result = getattr(outcome, "result", None)
            error = getattr(outcome, "error", None)
            for case in batch:
                item = result.results.get(case.clip8) if (error is None and result is not None) else None
                if item is None:
                    infra_failed += 1
                    rows.append({"clip8": case.clip8, "expected": case.expected_action,
                                 "predicted": None, "confidence": None, "match": False})
                    continue
                predicted = item.get("action")
                match = predicted == case.expected_action
                if match:
                    exact[case.expected_action] = exact.get(case.expected_action, 0) + 1
                if case.expected_action == "basking" and predicted == "unseen":
                    visible_basking_as_unseen += 1
                rows.append({"clip8": case.clip8, "expected": case.expected_action,
                             "predicted": predicted, "confidence": item.get("confidence"), "match": match})
    return CanarySummary(total=len(cases), infra_failed=infra_failed, exact_by_action=exact,
                         visible_basking_as_unseen=visible_basking_as_unseen, rows=rows)


def execute_canary(cases, video_dir, model, *, actual_host, expected_host, host_guard_fn,
                   auth_fn, lock_fn, release_fn, analyzer, extract_fn) -> CanarySummary:
    host_guard_fn(actual_host, expected_host)
    lock = lock_fn()
    if lock is None:
        raise RuntimeError("vlm_lock_busy")
    try:
        _verify_media(cases, video_dir)
        auth_fn()
        return evaluate_cases(cases, video_dir, model, analyzer=analyzer, extract_fn=extract_fn)
    finally:
        release_fn(lock)


def _default_analyzer(frame_sets, model):
    return analyze_batch_with_retry(frame_sets, model)


def main(argv=None, *, actual_host_fn=socket.gethostname, host_guard=require_expected_host,
         lock_fn=None, auth_fn=check_cli_auth) -> int:
    parser = argparse.ArgumentParser(description="VLM 휴식 blind canary (Mac mini 전용)")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--video-dir", required=True)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-host", default=_EXPECTED_HOST)
    args = parser.parse_args(argv)
    if lock_fn is None:
        from reporter.vlm_candidate_worker import acquire_vlm_lock, release_vlm_lock
        lock_fn = acquire_vlm_lock
        release_fn = release_vlm_lock
    else:
        release_fn = lambda _lock: None
    try:
        cases = load_manifest(Path(args.manifest))
    except (OSError, ValueError, KeyError) as exc:
        print(f"[basking-canary] manifest error: {type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        summary = execute_canary(
            cases, Path(args.video_dir), args.model,
            actual_host=actual_host_fn(), expected_host=args.expected_host,
            host_guard_fn=host_guard, auth_fn=auth_fn, lock_fn=lock_fn, release_fn=release_fn,
            analyzer=_default_analyzer, extract_fn=extract_six,
        )
    except Exception as exc:  # noqa: BLE001 — host/lock/media/auth 실패는 안전 code 로 종료
        print(f"[basking-canary] infra error: {type(exc).__name__}", file=sys.stderr)
        return 1
    Path(args.output).write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    ok = accepted(summary)
    print(f"[basking-canary] accepted={ok} total={summary.total} infra_failed={summary.infra_failed} "
          f"exact={summary.exact_by_action} basking_as_unseen={summary.visible_basking_as_unseen}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
