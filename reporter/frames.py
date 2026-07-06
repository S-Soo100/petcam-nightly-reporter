"""적응형 프레임 추출 — lab scripts/_extract_frames_clip.py 이식 (v4.0 레시피).

레시피 3요소(메모리 feedback_adaptive_frame_sampling):
  ① 간격기반 장수 clamp(round(dur/3.5), 6, 20) — 클립 길이 비례
  ② 구간중앙 위치 t=(i+0.5)*dur/n — t=0(행동 전)/꼬리/뒷부분손실 3대 결함 회피
  ③ no-upscale @1080 — 다운스케일만, 업스케일(가짜 보간픽셀)은 VLM 판단 교란 + 토큰 낭비

lab 은 ffmpeg -ss 시간 seek 을 씀. OpenCV CAP_PROP_POS_FRAMES seek 은 코덱 키프레임에 따라
부정확해서, 입력표현 동치(= 정확도 레버, 메모리 feedback_frames_beat_montage)를 유지하려면
ffmpeg 로 이식해야 한다. drift 방지 위해 lab 원본과 로직 동치로 유지 (config.LAB_PROMPT_VERSION).
"""
import subprocess
from pathlib import Path

import cv2

from reporter import config


def probe_duration(video: Path) -> float:
    """ffprobe 로 mp4 duration(초). lab analyze.probe_duration 동치 (실패 시 30.0 폴백).

    DB motion_clips.duration_sec 이 아니라 파일에서 직접 probe — 트리거 기록값과
    실제 인코딩 길이가 어긋날 수 있고, 프레임 위치 계산은 파일 기준이 정확.
    """
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)]
        ).decode().strip()
        return float(out) if out and out != "N/A" else 30.0
    except Exception:  # noqa: BLE001 — ffprobe 실패해도 폴백 길이로 추출은 진행
        return 30.0


def _adaptive_n(dur: float, interval: float, lo: int, hi: int) -> int:
    """간격당 1장 → 클립 길이 비례 장수. 짧은 클립 하한 / 긴 클립 상한 clamp."""
    return max(lo, min(hi, round(dur / interval)))


def _enforce_no_upscale(p: Path) -> None:
    """긴변 > FRAME_LONG_EDGE 면 다운스케일, 이하면 원본 유지(업스케일 금지)."""
    img = cv2.imread(str(p))
    h, w = img.shape[:2]
    if max(h, w) > config.FRAME_LONG_EDGE:
        s = config.FRAME_LONG_EDGE / max(h, w)
        img = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(p), img, [cv2.IMWRITE_JPEG_QUALITY, 95])


def extract_adaptive(video: Path, out_dir: Path) -> list[Path]:
    """적응형 프레임 추출. config 상수(interval 3.5 / clamp 6~20 / 1080) 사용.

    각 구간중앙 타임스탬프를 ffmpeg -ss 로 원본해상도 추출 후 no-upscale 다운스케일.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = probe_duration(video)
    n = _adaptive_n(dur, config.FRAME_INTERVAL_SEC, config.FRAME_MIN, config.FRAME_MAX)
    paths: list[Path] = []
    for i in range(n):
        t = (i + 0.5) * dur / n  # 구간 중앙
        p = out_dir / f"f_{i + 1:03d}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "3", str(p)],
            capture_output=True,
        )
        if p.exists():
            _enforce_no_upscale(p)
            paths.append(p)
    return paths
