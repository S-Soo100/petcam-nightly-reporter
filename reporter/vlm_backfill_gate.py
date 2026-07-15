from __future__ import annotations

import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from reporter import r2
from reporter.activity_worker import build_activity_policy
from reporter.gate_runner import assess_clip, load_detector
from reporter.vlm_models import CandidateClip


@dataclass(frozen=True, slots=True)
class GateEnrichment:
    clips: list[CandidateClip]
    snapshots: dict[str, dict[str, object]]
    stats: dict[str, int]


def _existing_snapshot(clip: CandidateClip) -> dict[str, object]:
    return {
        "source": "existing",
        "decision": clip.activity_decision,
        "gecko_visible": clip.gecko_visible,
        "visibility_confidence": clip.visibility_confidence,
        "gecko_bbox": list(clip.gecko_bbox) if clip.gecko_bbox else None,
        "motion_metrics": dict(clip.motion_metrics),
    }


def _assessment_snapshot(assessment) -> dict[str, object]:
    provenance = asdict(assessment.provenance) if assessment.provenance is not None else None
    return {
        "source": "ephemeral",
        "prelabel": assessment.result.to_dict(),
        "motion_metrics": asdict(assessment.motion),
        "decision": assessment.assessment.decision,
        "reason_code": assessment.assessment.reason_code,
        "measurements": dict(assessment.assessment.measurements),
        "provenance": provenance,
    }


def enrich_prepool(
    clips: list[CandidateClip],
    *,
    checkpoint: str,
    detector_factory=load_detector,
    download_fn=r2.download_clip,
    assess_fn=assess_clip,
) -> GateEnrichment:
    policy = build_activity_policy("activity-v1")
    detector = None
    enriched: list[CandidateClip] = []
    snapshots: dict[str, dict[str, object]] = {}
    stats = {"reused": 0, "assessed": 0, "failed": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for clip in clips:
            if clip.prelabel_id and clip.motion_metrics:
                enriched.append(clip)
                snapshots[clip.id] = _existing_snapshot(clip)
                stats["reused"] += 1
                continue
            destination = Path(tmp) / f"{clip.id}.mp4"
            try:
                if detector is None:
                    detector = detector_factory(checkpoint, policy.gate_threshold)
                video = download_fn(clip.r2_key, destination)
                gate = assess_fn(str(video), detector, policy, checkpoint, clip_id=clip.id)
                motion = asdict(gate.motion)
                bbox = tuple(gate.result.gecko_bbox) if gate.result.gecko_bbox else None
                enriched.append(replace(
                    clip,
                    activity_decision=gate.assessment.decision,
                    gecko_visible=gate.result.gecko_visible,
                    visibility_confidence=gate.result.visibility_confidence,
                    gecko_bbox=bbox,
                    motion_metrics=motion,
                ))
                snapshots[clip.id] = _assessment_snapshot(gate)
                stats["assessed"] += 1
            except Exception as exc:  # clip 단위 격리; 원문에는 URL/credential이 섞일 수 있어 type만 기록
                stats["failed"] += 1
                print(f"[vlm-backfill] gate clip={clip.id[:8]} error={type(exc).__name__}", file=sys.stderr)
            finally:
                destination.unlink(missing_ok=True)
    return GateEnrichment(enriched, snapshots, stats)
