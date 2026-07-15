"""30개 camera_clips를 Gate로 읽기만 하는 labeling triage preview CLI."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from supabase import create_client

from gecko_vision_gate.provenance import (
    SAMPLER_VERSION,
    SCHEMA_VERSION,
    GateProvenance,
    checkpoint_sha256,
)

from reporter import config, r2
from reporter.activity_worker import build_activity_policy
from reporter.gate_runner import assess_clip, load_detector, model_version_for
from reporter.labeling_triage_indexer import list_labeling_triage_candidates
from reporter.labeling_triage_models import LabelingTriageClip
from reporter.labeling_triage_policy import evidence_identity
from reporter.labeling_triage_worker import process_triage_batch

_DISPLAY_REASON = {
    "gate_active": "게코의 움직임이 감지됨",
    "gate_absent": "게코가 감지되지 않음",
    "gate_static": "게코가 보이지만 움직임이 거의 없음",
    "unknown": "자동 판단이 어려워 일반 큐에 유지",
}


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def select_preview_candidates(
    clips: list[LabelingTriageClip], limit: int
) -> list[LabelingTriageClip]:
    """(camera, UTC date, 6h bucket) strata를 한 건씩 순환한다."""
    groups: dict[tuple[str, str, int], deque[LabelingTriageClip]] = defaultdict(deque)
    for clip in sorted(clips, key=lambda c: (c.started_at, c.id)):
        captured = _parse_time(clip.started_at)
        groups[(clip.camera_id, captured.date().isoformat(), captured.hour // 6)].append(clip)
    selected: list[LabelingTriageClip] = []
    keys = sorted(groups)
    while len(selected) < limit and keys:
        next_keys = []
        for key in keys:
            if len(selected) >= limit:
                break
            selected.append(groups[key].popleft())
            if groups[key]:
                next_keys.append(key)
        keys = next_keys
    return selected


def write_preview_artifacts(output: Path, rows: list[dict], stats: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "preview.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    fields = list(rows[0]) if rows else [
        "clip8", "captured_at", "camera_id", "suggested_route", "suggestion_reason",
        "display_reason", "evidence_identity", "review_file", "owner_review",
    ]
    with (output / "preview.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# Labeling triage Preview 30",
        "",
        "> DB write 없이 만든 owner blind 검토 자료다. 영상만 보고 `라벨링 필요 / 라벨링 안 함 / 판단 어려움`을 기록해.",
        "",
        "## 실행 요약",
        "",
        *[f"- {key}: {value}" for key, value in stats.items()],
        "",
        "## 후보",
        "",
        "| # | clip8 | 촬영 시각 | 카메라 | 시스템 제안 | 쉬운 사유 | 검토 영상 | owner 판정 |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"| {i} | {row['clip8']} | {row['captured_at']} | {row['camera_id'][:8]} | "
            f"{row['suggested_route']} | {row['display_reason']} | [{row['review_file']}]({row['review_file']}) |  |"
        )
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_preview(
    *,
    start: datetime,
    end: datetime,
    limit: int,
    output: Path,
    sb=None,
    list_candidates_fn=list_labeling_triage_candidates,
    load_detector_fn=load_detector,
    download_fn=r2.download_clip,
    assess_fn=assess_clip,
) -> int:
    sb = sb or create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    policy = build_activity_policy(config.LABELING_TRIAGE_ACTIVITY_POLICY_VERSION)
    policy_version = config.LABELING_TRIAGE_POLICY_VERSION
    checkpoint = config.GATE_CHECKPOINT_PATH
    frames = config.LABELING_TRIAGE_FRAMES
    provenance = GateProvenance(
        model_name="RF-DETR",
        model_version=model_version_for(checkpoint),
        checkpoint_sha256=checkpoint_sha256(checkpoint),
        threshold=policy.gate_threshold,
        sampler_version=SAMPLER_VERSION,
        schema_version=SCHEMA_VERSION,
        frames_sampled=frames,
    )
    pool = list_candidates_fn(
        sb, start=start, end=end, limit=max(limit * 10, limit), page_size=500,
        identity_for_clip=lambda clip_id: evidence_identity(clip_id, provenance, policy_version),
    )
    selected = select_preview_candidates(pool, limit)
    if len(selected) != limit:
        raise RuntimeError(f"preview requires {limit} candidates, found {len(selected)}")
    output.mkdir(parents=True, exist_ok=True)
    review_dir = output / "review"
    review_dir.mkdir(exist_ok=True)
    rows: list[dict] = []

    def collect(clip, _gate, suggestion, video_path: Path) -> None:
        filename = f"{len(rows) + 1:02d}-{clip.id[:8]}.mp4"
        relative = Path("review") / filename
        shutil.copy2(video_path, output / relative)
        reason = suggestion.suggestion_reason if suggestion is not None else "unknown"
        rows.append({
            "clip8": clip.id[:8],
            "captured_at": clip.started_at,
            "camera_id": clip.camera_id,
            "suggested_route": suggestion.suggested_route if suggestion is not None else "label",
            "suggestion_reason": reason,
            "display_reason": _DISPLAY_REASON[reason],
            "evidence_identity": (
                suggestion.evidence_snapshot["identity"]
                if suggestion is not None else evidence_identity(clip.id, _gate.provenance, policy_version)
            ),
            "review_file": relative.as_posix(),
            "owner_review": "",
        })

    detector = load_detector_fn(checkpoint, policy.gate_threshold)
    stats = process_triage_batch(
        sb, selected, detector, policy, checkpoint, policy_version,
        write_enabled=False, download_fn=download_fn, assess_fn=assess_fn,
        store_fn=lambda *_args: (_ for _ in ()).throw(AssertionError("preview DB write")),
        on_assessed=collect, num_frames=frames,
    )
    write_preview_artifacts(output, rows, stats)
    if stats["assessed"] != len(selected) or len(rows) != len(selected):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return run_preview(
        start=_parse_time(args.start), end=_parse_time(args.end),
        limit=args.limit, output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
