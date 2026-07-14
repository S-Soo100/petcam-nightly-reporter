import math,subprocess
from pathlib import Path
import cv2
from reporter.frames import probe_duration

class FrameExtractionError(RuntimeError):pass
def sample_times(duration):
    if not math.isfinite(duration) or duration<=0:raise FrameExtractionError("positive duration required")
    return [(i+.5)*duration/6 for i in range(6)]
def normalize_jpeg(path):
    img=cv2.imread(str(path))
    if img is None:raise FrameExtractionError(f"cannot decode {path}")
    h,w=img.shape[:2]
    if max(w,h)>768:
        s=768/max(w,h);img=cv2.resize(img,(round(w*s),round(h*s)),interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path),img,[cv2.IMWRITE_JPEG_QUALITY,85]);h,w=img.shape[:2];return w,h
def extract_six(video,out_dir):
    out_dir.mkdir(parents=True,exist_ok=True);paths=[]
    for i,t in enumerate(sample_times(probe_duration(video))):
        p=out_dir/f"f_{i+1:03d}.jpg";subprocess.run(["ffmpeg","-y","-ss",f"{t:.3f}","-i",str(video),"-frames:v","1","-q:v","3",str(p)],capture_output=True)
        if p.exists():normalize_jpeg(p);paths.append(p)
    if len(paths)!=6:raise FrameExtractionError(f"expected 6, got {len(paths)}")
    return paths
