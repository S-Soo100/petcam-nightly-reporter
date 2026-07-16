"""gate_runner.assess_clip — Gate 부품 조합(sample→prelabel→motion→decide→provenance).

detector 는 주입(1회 로드 재사용). sample_fn 주입으로 실제 mp4 없이 파이프라인 검증.
"""

from __future__ import annotations

import numpy as np

import pytest

from gecko_vision_gate.activity_policy import DECISIONS, ActivityPolicy
from gecko_vision_gate.detector import RawDetection

from reporter.gate_runner import InsufficientSampleFrames, assess_clip, model_version_for


class _FakeDet:
    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    def detect(self, _frame):
        out = self._seq[self._i] if self._i < len(self._seq) else []
        self._i += 1
        return out


class _CountingDet:
    """detect() 호출 여부를 검증하기 위한 detector (호출 시 count 증가)."""

    def __init__(self):
        self.calls = 0

    def detect(self, _frame):
        self.calls += 1
        return []


def _frames(n=2):
    return [(float(i), np.zeros((20, 20, 3), np.uint8)) for i in range(n)]


def test_assess_clip_full_pipeline(tmp_path):
    ck = tmp_path / "checkpoint_best_ema.pth"
    ck.write_bytes(b"weights")
    det = _FakeDet([[RawDetection("gecko", 0.9, [2, 2, 5, 5])],
                   [RawDetection("gecko", 0.85, [2, 2, 5, 5])]])
    pol = ActivityPolicy(version="pol-v0", gate_threshold=0.25, min_frames=2, sparse_min_visible=2)
    ga = assess_clip("x.mp4", det, pol, str(ck), num_frames=2, clip_id="c1",
                     sample_fn=lambda p, n: _frames(2))
    assert ga.result.gecko_visible is True
    assert ga.result.clip_id == "c1"
    assert ga.motion.visible_frame_count == 2
    assert ga.assessment.decision in DECISIONS
    # provenance 실측
    assert ga.provenance.threshold == 0.25
    assert len(ga.provenance.checkpoint_sha256) == 64  # sha256 hex
    assert ga.provenance.frames_sampled == 2


def test_absent_clip_decision(tmp_path):
    ck = tmp_path / "checkpoint_best_ema.pth"
    ck.write_bytes(b"w")
    det = _FakeDet([[], []])  # gecko 없음
    pol = ActivityPolicy(version="pol-v0", gate_threshold=0.25, min_frames=2)
    ga = assess_clip("x.mp4", det, pol, str(ck), num_frames=2, sample_fn=lambda p, n: _frames(2))
    assert ga.result.gecko_visible is False
    assert ga.assessment.decision == "exclude_absent"


def test_assess_clip_rejects_zero_frames_before_detector(tmp_path):
    # 0 프레임 → detector 도달 전에 InsufficientSampleFrames raise
    ck = tmp_path / "checkpoint_best_ema.pth"
    ck.write_bytes(b"w")
    det = _CountingDet()
    pol = ActivityPolicy(version="activity-v1", gate_threshold=0.1)  # min_frames=6
    with pytest.raises(InsufficientSampleFrames) as exc:
        assess_clip("secret/path.mp4", det, pol, str(ck), num_frames=12,
                    sample_fn=lambda p, n: _frames(0))
    assert det.calls == 0  # detector 미호출
    assert exc.value.found == 0 and exc.value.required == 6
    assert "secret" not in str(exc.value) and ".mp4" not in str(exc.value)  # 경로 미노출


def test_assess_clip_rejects_five_frames_before_storeable_result(tmp_path):
    # 5 프레임(< min 6) → 저장 가능한 결과 조립 전에 reject
    ck = tmp_path / "checkpoint_best_ema.pth"
    ck.write_bytes(b"w")
    det = _CountingDet()
    pol = ActivityPolicy(version="activity-v1", gate_threshold=0.1)
    with pytest.raises(InsufficientSampleFrames) as exc:
        assess_clip("x.mp4", det, pol, str(ck), num_frames=12,
                    sample_fn=lambda p, n: _frames(5))
    assert det.calls == 0
    assert exc.value.found == 5 and exc.value.required == 6


def test_assess_clip_accepts_six_frames(tmp_path):
    # 정확히 min_frames=6 이면 정상 파이프라인 통과(raise 없음)
    ck = tmp_path / "checkpoint_best_ema.pth"
    ck.write_bytes(b"w")
    det = _FakeDet([[]] * 6)
    pol = ActivityPolicy(version="activity-v1", gate_threshold=0.1)
    ga = assess_clip("x.mp4", det, pol, str(ck), num_frames=12,
                     sample_fn=lambda p, n: _frames(6))
    assert ga.provenance.frames_sampled == 6
    assert ga.assessment.decision in DECISIONS


def test_model_version_for_checkpoint_path():
    assert model_version_for("runs/gecko_v2/checkpoint_best_ema.pth") == "gecko_v2 (checkpoint_best_ema)"
