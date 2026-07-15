from datetime import datetime, timezone
from pathlib import Path

from gecko_vision_gate.activity_policy import ActivityAssessment
from gecko_vision_gate.motion_evidence import MotionMetrics
from gecko_vision_gate.schema import PrelabelResult

from reporter.gate_runner import GateAssessment
from reporter.vlm_backfill_gate import enrich_prepool
from reporter.vlm_models import CandidateClip


def _clip(cid: str, *, existing: bool) -> CandidateClip:
    return CandidateClip(
        id=cid,
        camera_id="camera-a",
        started_at=datetime(2026, 7, 7, 11, tzinfo=timezone.utc),
        duration_sec=30,
        r2_key=f"clips/{cid}.mp4",
        motion_score=1,
        width=1280,
        height=720,
        prelabel_id="prelabel-1" if existing else None,
        activity_decision="active" if existing else None,
        gecko_visible=True if existing else None,
        visibility_confidence=0.9 if existing else None,
        gecko_bbox=(10, 20, 30, 40) if existing else None,
        motion_metrics={"roi_flow_mag": 1.2, "global_bg_change": 0.1} if existing else {},
    )


def _must_not_call(*_args, **_kwargs):
    raise AssertionError("must not be called")


def _assessment(cid: str) -> GateAssessment:
    result = PrelabelResult(
        gecko_visible=True,
        visibility_confidence=0.8,
        frames_sampled=12,
        model_name="rfdetr",
        model_version="checkpoint",
        gecko_bbox=[11, 22, 33, 44],
        clip_id=cid,
    )
    motion = MotionMetrics(8, 0.67, 0.04, 0.1, 0.7, 0.8, 0.2, False)
    assessment = ActivityAssessment("active", "motion_observed", {"policy_version": "activity-v1"})
    return GateAssessment(result, motion, assessment, provenance=None)  # type: ignore[arg-type]


def test_existing_evidence_is_reused_without_download_or_detector():
    result = enrich_prepool(
        [_clip("existing", existing=True)],
        checkpoint="checkpoint.pth",
        detector_factory=_must_not_call,
        download_fn=_must_not_call,
        assess_fn=_must_not_call,
    )
    assert result.stats == {"reused": 1, "assessed": 0, "failed": 0}
    assert result.snapshots["existing"]["source"] == "existing"


def test_missing_evidence_loads_detector_once_and_cleans_temp_files():
    calls = {"detector": 0, "assess": 0}
    destinations: list[Path] = []

    def detector_factory(_checkpoint, _threshold):
        calls["detector"] += 1
        return object()

    def download_fn(_key, dest):
        destinations.append(dest)
        dest.write_bytes(b"mp4")
        return dest

    def assess_fn(_path, _detector, _policy, _checkpoint, *, clip_id):
        calls["assess"] += 1
        return _assessment(clip_id)

    result = enrich_prepool(
        [_clip("raw-a", existing=False), _clip("raw-b", existing=False)],
        checkpoint="checkpoint.pth",
        detector_factory=detector_factory,
        download_fn=download_fn,
        assess_fn=assess_fn,
    )
    assert calls == {"detector": 1, "assess": 2}
    assert result.stats == {"reused": 0, "assessed": 2, "failed": 0}
    assert all(not path.exists() for path in destinations)
    assert all(clip.activity_decision == "active" for clip in result.clips)


def test_one_gate_failure_is_isolated_and_temp_file_is_removed():
    destinations: list[Path] = []

    def download_fn(_key, dest):
        destinations.append(dest)
        dest.write_bytes(b"mp4")
        return dest

    def assess_fn(_path, _detector, _policy, _checkpoint, *, clip_id):
        if clip_id == "bad":
            raise RuntimeError("decode")
        return _assessment(clip_id)

    result = enrich_prepool(
        [_clip("good", existing=False), _clip("bad", existing=False)],
        checkpoint="checkpoint.pth",
        detector_factory=lambda *_args: object(),
        download_fn=download_fn,
        assess_fn=assess_fn,
    )
    assert result.stats == {"reused": 0, "assessed": 1, "failed": 1}
    assert [clip.id for clip in result.clips] == ["good"]
    assert all(not path.exists() for path in destinations)
