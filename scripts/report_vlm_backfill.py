#!/usr/bin/env python3
"""VLM 240개 백필 최종 보고 — REPORT.md, jobs.json, source night별 contact sheet 8개.

전체 clip UUID/owner ID/이메일/R2 credential은 어떤 artifact에도 쓰지 않는다(clip8만 사용).
원본 mp4는 TemporaryDirectory에서만 받아 즉시 삭제한다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reporter import config, r2  # noqa: E402
from reporter.frames import probe_duration  # noqa: E402
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION, source_nights  # noqa: E402

CONTACT_SHEET_COLS = 5
CONTACT_SHEET_ROWS = 6
_CELL_W, _CELL_H = 240, 180
KST = ZoneInfo("Asia/Seoul")


class ThumbnailError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Report:
    total: int
    by_date: dict[str, int]
    by_slot: dict[str, int]
    by_action: dict[str, int]
    by_status: dict[str, int]
    by_model: dict[str, int]
    actual_cost_usd: float
    equivalent_cost_usd: float
    error_counts: dict[str, int]
    model_mismatch_count: int


def validate_output_path(path: Path, *, repo_root: Path = config.REPO_ROOT) -> Path:
    """--out은 repo storage/ 아래여야 한다. 사용자 storage 데이터를 벗어난 곳에 쓰지 않기 위한 안전장치."""
    resolved = Path(path).expanduser().resolve()
    storage_root = (repo_root / "storage").resolve()
    try:
        resolved.relative_to(storage_root)
    except ValueError as exc:
        raise ValueError(f"--out must stay under repo storage/: {resolved}") from exc
    return resolved


def _job_date(job: dict) -> str:
    rank_features = job.get("rank_features") or {}
    source_date = rank_features.get("source_date")
    if source_date:
        return source_date
    window_start = datetime.fromisoformat(str(job["window_start"]).replace("Z", "+00:00"))
    local = window_start.astimezone(KST)
    day = local.date() if local.hour >= 20 else local.date() - timedelta(days=1)
    return day.isoformat()


def aggregate(jobs: list[dict]) -> Report:
    by_date: Counter = Counter()
    by_slot: Counter = Counter()
    by_action: Counter = Counter()
    by_status: Counter = Counter()
    by_model: Counter = Counter()
    error_counts: Counter = Counter()
    actual_cost = 0.0
    equivalent_cost = 0.0
    model_mismatch = 0
    for job in jobs:
        by_date[_job_date(job)] += 1
        by_slot[job.get("slot") or "unknown"] += 1
        result = job.get("result") or {}
        by_action[result.get("action") or job.get("status") or "unknown"] += 1
        by_status[job.get("status") or "unknown"] += 1
        model_actual = job.get("model_actual")
        if model_actual:
            by_model[model_actual] += 1
            model_requested = job.get("model_requested")
            if model_requested and model_requested != model_actual:
                model_mismatch += 1
        actual_cost += float(job.get("cost_usd") or 0)
        equivalent_cost += float(result.get("provider_estimated_cost_usd") or 0)
        error_code = job.get("error_code")
        if error_code:
            error_counts[error_code] += 1
    return Report(
        total=len(jobs), by_date=dict(by_date), by_slot=dict(by_slot), by_action=dict(by_action),
        by_status=dict(by_status), by_model=dict(by_model), actual_cost_usd=actual_cost,
        equivalent_cost_usd=equivalent_cost, error_counts=dict(error_counts),
        model_mismatch_count=model_mismatch,
    )


def _sanitize_job(job: dict) -> dict:
    """artifact에 남길 안전한 필드만 — 전체 UUID/owner/email/credential 제외."""
    result = job.get("result") or {}
    return {
        "clip8": (job.get("clip_id") or "")[:8],
        "date": _job_date(job),
        "bucket_index": (job.get("rank_features") or {}).get("bucket_index"),
        "slot": job.get("slot"),
        "status": job.get("status"),
        "action": result.get("action"),
        "confidence": result.get("confidence"),
        "model_requested": job.get("model_requested"),
        "model_actual": job.get("model_actual"),
        "cost_usd": float(job.get("cost_usd") or 0),
        "equivalent_cost_usd": float(result.get("provider_estimated_cost_usd") or 0),
        "error_code": job.get("error_code"),
    }


def _fmt_table(counts: dict[str, object]) -> str:
    if not counts:
        return "(없음)\n"
    lines = [f"| {key} | {value} |" for key, value in sorted(counts.items(), key=lambda kv: str(kv[0]))]
    return "| key | count |\n| --- | --- |\n" + "\n".join(lines) + "\n"


def render_report_md(report: Report, jobs: list[dict]) -> str:
    complete = "예" if report.total == 240 else "아니오"
    lines = [
        "# VLM 240개 과거 백필 최종 보고",
        "",
        f"- selector_version: `{BACKFILL_SELECTOR_VERSION}`",
        f"- 총 job 수: {report.total}",
        f"- 240 완료 여부: {complete}",
        f"- 실제 비용(actual_cost_usd): ${report.actual_cost_usd:.2f}",
        f"- API 환산 참고비용(equivalent_cost_usd): ${report.equivalent_cost_usd:.2f}",
        f"- 요청/실제 모델 불일치: {report.model_mismatch_count}",
        "",
        "## 날짜별",
        _fmt_table(report.by_date),
        "## slot별",
        _fmt_table(report.by_slot),
        "## action별",
        _fmt_table(report.by_action),
        "## status별 (terminal/held 포함)",
        _fmt_table(report.by_status),
        "## model_actual별",
        _fmt_table(report.by_model),
        "## error_code별",
        _fmt_table(report.error_counts),
        "## contact sheet",
        "\n".join(f"- `contact-sheet-{date}.jpg`" for date in sorted({_job_date(job) for job in jobs})),
        "",
    ]
    return "\n".join(lines) + "\n"


def default_thumbnail(video: Path) -> np.ndarray:
    """clip 대표 프레임(중앙 시점) 1장을 ndarray로 반환. 임시 jpg는 함수 내부에서만 존재."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "thumb.jpg"
        duration = probe_duration(video)
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{duration / 2:.3f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "5", str(out)],
            capture_output=True,
        )
        img = cv2.imread(str(out))
        if img is None:
            raise ThumbnailError(f"cannot extract thumbnail from {video}")
        return img


def _label_text(job: dict) -> str:
    result = job.get("result") or {}
    clip8 = (job.get("clip_id") or "")[:8]
    slot = (job.get("slot") or "?")[:4]
    action = result.get("action") or job.get("status") or "?"
    confidence = result.get("confidence")
    conf_text = f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "-"
    return f"{clip8} {slot} {action} {conf_text}"


def _labeled_cell(img: np.ndarray, label: str) -> np.ndarray:
    cell = cv2.resize(img, (_CELL_W, _CELL_H), interpolation=cv2.INTER_AREA)
    cv2.rectangle(cell, (0, _CELL_H - 18), (_CELL_W, _CELL_H), (0, 0, 0), -1)
    cv2.putText(cell, label, (2, _CELL_H - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (255, 255, 255), 1, cv2.LINE_AA)
    return cell


def _blank_cell(label: str) -> np.ndarray:
    cell = np.zeros((_CELL_H, _CELL_W, 3), dtype=np.uint8)
    cv2.putText(cell, label, (2, _CELL_H - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (128, 128, 128), 1, cv2.LINE_AA)
    return cell


def build_contact_sheet(
    jobs_for_night: list[dict],
    *,
    r2_key_lookup_fn,
    download_fn=r2.download_clip,
    thumbnail_fn=default_thumbnail,
) -> np.ndarray:
    """source night 하나의 최대 30개를 5x6 grid로. mp4는 TemporaryDirectory에서만 쓰고 즉시 삭제."""
    capacity = CONTACT_SHEET_COLS * CONTACT_SHEET_ROWS
    cells: list[np.ndarray] = []
    with tempfile.TemporaryDirectory() as tmp:
        for job in jobs_for_night[:capacity]:
            clip_id = job.get("clip_id") or ""
            dest = Path(tmp) / f"{clip_id}.mp4"
            try:
                download_fn(r2_key_lookup_fn(clip_id), dest)
                img = thumbnail_fn(dest)
                cells.append(_labeled_cell(img, _label_text(job)))
            except Exception:
                cells.append(_blank_cell(f"{clip_id[:8]} ERR"))
            finally:
                dest.unlink(missing_ok=True)
    while len(cells) < capacity:
        cells.append(_blank_cell(""))
    rows = [
        np.hstack(cells[row * CONTACT_SHEET_COLS:(row + 1) * CONTACT_SHEET_COLS])
        for row in range(CONTACT_SHEET_ROWS)
    ]
    return np.vstack(rows)


def _default_r2_key_lookup(sb):
    def lookup(clip_ids: list[str]) -> dict[str, str]:
        if not clip_ids:
            return {}
        rows = sb.table("motion_clips").select("id,r2_key").in_("id", clip_ids).execute().data
        return {row["id"]: row["r2_key"] for row in rows}
    return lookup


def write_report(
    jobs: list[dict],
    *,
    out_dir: Path,
    r2_key_lookup_fn=None,
    sb=None,
    download_fn=r2.download_clip,
    thumbnail_fn=default_thumbnail,
) -> Report:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = aggregate(jobs)
    (out_dir / "REPORT.md").write_text(render_report_md(report, jobs))
    (out_dir / "jobs.json").write_text(
        json.dumps([_sanitize_job(job) for job in jobs], ensure_ascii=False, indent=2) + "\n"
    )
    if r2_key_lookup_fn is None:
        keys_by_id = _default_r2_key_lookup(sb)([job["clip_id"] for job in jobs])
        r2_key_lookup_fn = lambda clip_id: keys_by_id.get(clip_id, "")  # noqa: E731
    by_date: dict[str, list[dict]] = defaultdict(list)
    for job in jobs:
        by_date[_job_date(job)].append(job)
    for source_date, night_jobs in sorted(by_date.items()):
        sheet = build_contact_sheet(
            night_jobs, r2_key_lookup_fn=r2_key_lookup_fn, download_fn=download_fn, thumbnail_fn=thumbnail_fn,
        )
        cv2.imwrite(str(out_dir / f"contact-sheet-{source_date}.jpg"), sheet)
    return report


def _load_jobs(sb) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    page_size = 1000
    while True:
        page = (
            sb.table("clip_vlm_jobs").select("*")
            .eq("selector_version", BACKFILL_SELECTOR_VERSION)
            .order("window_start").range(offset, offset + page_size - 1).execute().data
        )
        rows += page
        if len(page) < page_size:
            return rows
        offset += page_size


def main(argv=None, *, sb=None, download_fn=r2.download_clip, thumbnail_fn=default_thumbnail) -> int:
    parser = argparse.ArgumentParser(description="VLM 240개 백필 최종 보고")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    out_dir = validate_output_path(args.out)
    if sb is None:
        from supabase import create_client
        sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    jobs = _load_jobs(sb)
    report = write_report(jobs, out_dir=out_dir, sb=sb, download_fn=download_fn, thumbnail_fn=thumbnail_fn)
    print(
        f"report_saved={out_dir} total={report.total} actual_cost_usd={report.actual_cost_usd:.2f} "
        f"equivalent_cost_usd={report.equivalent_cost_usd:.2f} model_mismatch={report.model_mismatch_count}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
