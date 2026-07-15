"""camera_clips → Gate → 라벨링 triage 제안 worker (Claude/VLM 0회)."""

from __future__ import annotations

import fcntl
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from supabase import create_client

from gecko_vision_gate.activity_policy import ActivityPolicy
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
from reporter.labeling_triage_policy import evidence_identity, suggest_from_gate
from reporter.labeling_triage_store import store_triage_suggestion

_LOCK_PATH = "/tmp/petcam-labeling-triage-worker.lock"


def acquire_triage_lock():
    lock = open(_LOCK_PATH, "w")  # noqa: SIM115 — process lifetime flock
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except BlockingIOError:
        lock.close()
        return None


def release_triage_lock(lock) -> None:
    if lock is not None:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def _empty_stats(queried: int) -> dict[str, int]:
    return {
        "queried": queried,
        "assessed": 0,
        "stored_label": 0,
        "stored_quarantine_absent": 0,
        "stored_quarantine_static": 0,
        "reused": 0,
        "unknown": 0,
        "protected_session": 0,
        "failed_download": 0,
        "failed_gate": 0,
        "failed_store": 0,
        "temp_files_remaining": 0,
    }


def process_triage_batch(
    sb,
    clips,
    detector,
    policy: ActivityPolicy,
    checkpoint_path: str,
    policy_version: str,
    *,
    write_enabled: bool,
    download_fn,
    assess_fn,
    store_fn=store_triage_suggestion,
    on_assessed=None,
    num_frames: int = 12,
) -> dict[str, int]:
    stats = _empty_stats(len(clips))
    with tempfile.TemporaryDirectory(prefix="petcam-labeling-triage-") as tmp:
        tmp_path = Path(tmp)
        for clip in clips:
            dest = tmp_path / f"{clip.id}.mp4"
            try:
                download_fn(clip.r2_key, dest)
            except Exception as exc:  # noqa: BLE001 — 한 clip fail-open, 비밀 오류문구 출력 금지
                stats["failed_download"] += 1
                print(f"[labeling-triage] clip={clip.id[:8]} download_failed type={type(exc).__name__}",
                      file=sys.stderr, flush=True)
                dest.unlink(missing_ok=True)
                continue
            try:
                gate = assess_fn(
                    str(dest), detector, policy, checkpoint_path,
                    num_frames=num_frames, clip_id=clip.id,
                )
                stats["assessed"] += 1
                suggestion = suggest_from_gate(clip, gate, policy_version)
                if suggestion is None:
                    stats["unknown"] += 1
                if on_assessed is not None:
                    on_assessed(clip, gate, suggestion, dest)
            except Exception as exc:  # noqa: BLE001 — decode/Gate 한 clip 격리
                stats["failed_gate"] += 1
                print(f"[labeling-triage] clip={clip.id[:8]} gate_failed type={type(exc).__name__}",
                      file=sys.stderr, flush=True)
                dest.unlink(missing_ok=True)
                continue
            finally:
                if not write_enabled:
                    dest.unlink(missing_ok=True)
            if suggestion is None or not write_enabled:
                dest.unlink(missing_ok=True)
                continue
            try:
                result = store_fn(sb, suggestion)
            except Exception:
                stats["failed_store"] += 1
                dest.unlink(missing_ok=True)
                raise  # DB/RPC 전역 장애는 배치를 성공처럼 계속하지 않는다.
            if result.status == "stored":
                key = {
                    "gate_active": "stored_label",
                    "gate_absent": "stored_quarantine_absent",
                    "gate_static": "stored_quarantine_static",
                }[suggestion.suggestion_reason]
                stats[key] += 1
            elif result.status == "reused":
                stats["reused"] += 1
            elif result.status == "protected_session":
                stats["protected_session"] += 1
            dest.unlink(missing_ok=True)
        stats["temp_files_remaining"] = sum(1 for p in tmp_path.rglob("*") if p.is_file())
    return stats


def run(
    *,
    sb=None,
    now: datetime | None = None,
    enabled: bool | None = None,
    write_enabled: bool | None = None,
    create_client_fn=create_client,
    list_candidates_fn=list_labeling_triage_candidates,
    load_detector_fn=load_detector,
    download_fn=r2.download_clip,
    assess_fn=assess_clip,
    store_fn=store_triage_suggestion,
    acquire_lock_fn=acquire_triage_lock,
    release_lock_fn=release_triage_lock,
    on_assessed=None,
    candidate_limit: int | None = None,
) -> int:
    enabled = config.LABELING_TRIAGE_ENABLED if enabled is None else enabled
    if not enabled:
        print("[labeling-triage] disabled — skip", flush=True)
        return 0
    write_enabled = (
        config.LABELING_TRIAGE_WRITE_ENABLED if write_enabled is None else write_enabled
    )
    lock = acquire_lock_fn()
    if lock is None:
        print("[labeling-triage] already running — skip", flush=True)
        return 0
    try:
        now = now or datetime.now(timezone.utc)
        sb = sb or create_client_fn(config.SUPABASE_URL, config.SUPABASE_KEY)
        checkpoint = config.GATE_CHECKPOINT_PATH
        policy = build_activity_policy(config.LABELING_TRIAGE_ACTIVITY_POLICY_VERSION)
        policy_version = config.LABELING_TRIAGE_POLICY_VERSION
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
        clips = list_candidates_fn(
            sb,
            start=now - timedelta(hours=config.LABELING_TRIAGE_WINDOW_HOURS),
            end=now,
            limit=candidate_limit or config.LABELING_TRIAGE_BATCH_LIMIT,
            identity_for_clip=lambda clip_id: evidence_identity(clip_id, provenance, policy_version),
        )
        if not clips:
            print("[labeling-triage] no candidates", flush=True)
            return 0
        detector = load_detector_fn(checkpoint, policy.gate_threshold)
        stats = process_triage_batch(
            sb, clips, detector, policy, checkpoint, policy_version,
            write_enabled=write_enabled, download_fn=download_fn, assess_fn=assess_fn,
            store_fn=store_fn, on_assessed=on_assessed, num_frames=frames,
        )
        print(
            "[labeling-triage] " + " ".join(f"{key}={value}" for key, value in stats.items())
            + f" write_enabled={int(write_enabled)} policy={policy_version}",
            flush=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI는 비밀 없는 타입만 출력하고 nonzero
        print(f"[labeling-triage] batch_failed type={type(exc).__name__}", file=sys.stderr, flush=True)
        return 1
    finally:
        release_lock_fn(lock)


if __name__ == "__main__":
    raise SystemExit(run())
