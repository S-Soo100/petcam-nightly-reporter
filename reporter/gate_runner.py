"""Gate 부품(gecko-vision-gate) 을 한 clip 파이프라인으로 조합 — 오케스트레이터 레이어.

Gate 는 evidence 부품(sample/prelabel/motion) + policy 순수함수만 제공하고, 여기서
sample→prelabel→motion→decide→provenance 로 엮는다. detector 는 worker 가 1회 로드해서
주입한다(process lifetime 재사용, subprocess/재로드 금지 — 지시문 §135·§366).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gecko_vision_gate.activity_policy import ActivityAssessment, ActivityPolicy, decide
from gecko_vision_gate.detector import GeckoDetector
from gecko_vision_gate.frame_sampling import sample_frames
from gecko_vision_gate.motion_evidence import MotionMetrics, compute_motion_metrics
from gecko_vision_gate.prelabel import prelabel_from_frames
from gecko_vision_gate.provenance import (
    SAMPLER_VERSION,
    SCHEMA_VERSION,
    GateProvenance,
    checkpoint_sha256,
)
from gecko_vision_gate.schema import PrelabelResult


class InsufficientSampleFrames(Exception):
    """샘플된 프레임이 policy 최소치 미만 — 저장 가능한 evidence 를 조립하지 않는다.

    Gate sampler(순차 fallback 포함)가 실제로 읽은 프레임이 `policy.min_frames` 미만이면,
    detector·DB store 에 도달하기 전에 이 예외를 던져 불완전 evidence 가 완료로 굳는 것을 막는다.
    로그 위생상 파일 경로를 담지 않고 개수만 노출한다(§5.3).
    """

    def __init__(self, found: int, required: int):
        self.found = found
        self.required = required
        super().__init__(f"insufficient sample frames: found={found} required={required}")


@dataclass(frozen=True, slots=True)
class GateAssessment:
    """한 clip 의 evidence + motion + decision + provenance 묶음 (store 로 넘어감)."""

    result: PrelabelResult
    motion: MotionMetrics
    assessment: ActivityAssessment
    provenance: GateProvenance


def load_detector(checkpoint_path: str, threshold: float, model_size: str = "nano") -> GeckoDetector:
    """detector 1회 로드(모델은 첫 detect 에서 lazy). worker 가 batch 전에 한 번 호출."""
    return GeckoDetector(model_size=model_size, threshold=threshold, checkpoint=checkpoint_path)


def model_version_for(checkpoint_path: str, model_size: str = "nano") -> str:
    """prelabel 과 동일 공식으로 model_version 을 미리 계산(indexer 미처리 판별용)."""
    p = Path(checkpoint_path)
    return f"{p.parent.name} ({p.stem})"


def assess_clip(
    video_path,
    detector: GeckoDetector,
    policy: ActivityPolicy,
    checkpoint_path: str,
    *,
    num_frames: int = 12,
    model_size: str = "nano",
    clip_id: str | None = None,
    sample_fn=sample_frames,
) -> GateAssessment:
    """mp4 → 균등 샘플 → evidence → motion → four-state 판정 → provenance."""
    frames = sample_fn(video_path, num_frames)
    # 최소 프레임 barrier: fallback 이후에도 min_frames 미달이면 detector·store 도달 전에 중단.
    # 불완전(부분/빈) 샘플을 정상 evidence 로 조립하지 않는다(설계 §4.3). process_batch 가
    # 예외를 잡아 failed 로 집계하고 다음 clip 을 계속 처리하며, cycle 은 nonzero 로 끝난다.
    if len(frames) < policy.min_frames:
        raise InsufficientSampleFrames(found=len(frames), required=policy.min_frames)
    result = prelabel_from_frames(
        frames,
        threshold=policy.gate_threshold,
        model_size=model_size,
        checkpoint=checkpoint_path,
        clip_id=clip_id,
        detector=detector,
    )
    motion = compute_motion_metrics(frames, result)
    assessment = decide(result, motion, policy)
    provenance = GateProvenance(
        model_name=result.model_name,
        model_version=result.model_version,
        checkpoint_sha256=checkpoint_sha256(str(checkpoint_path)),
        threshold=policy.gate_threshold,
        sampler_version=SAMPLER_VERSION,
        schema_version=SCHEMA_VERSION,
        frames_sampled=result.frames_sampled,
    )
    return GateAssessment(result, motion, assessment, provenance)
