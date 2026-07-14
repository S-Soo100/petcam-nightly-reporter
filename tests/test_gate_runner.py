"""gate_runner.assess_clip — Gate 부품 조합(sample→prelabel→motion→decide→provenance).

detector 는 주입(1회 로드 재사용). sample_fn 주입으로 실제 mp4 없이 파이프라인 검증.
"""

from __future__ import annotations

import numpy as np

from gecko_vision_gate.activity_policy import DECISIONS, ActivityPolicy
from gecko_vision_gate.detector import RawDetection

from reporter.gate_runner import assess_clip, model_version_for


class _FakeDet:
    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    def detect(self, _frame):
        out = self._seq[self._i] if self._i < len(self._seq) else []
        self._i += 1
        return out


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


def test_model_version_for_checkpoint_path():
    assert model_version_for("runs/gecko_v2/checkpoint_best_ema.pth") == "gecko_v2 (checkpoint_best_ema)"
