"""워커 전역 설정. .env 는 이 패키지 부모(레포 루트)에서 로드."""
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
WINDOW_HOURS = float(os.environ.get("WINDOW_HOURS", "2"))

# 적응형 프레임 추출 (lab 레시피 v4.0 기준 — feedback_adaptive_frame_sampling)
FRAME_INTERVAL_SEC = 3.5
FRAME_MIN = 6
FRAME_MAX = 20
FRAME_LONG_EDGE = 1080  # no-upscale cap

# 이식한 lab 자산 버전 (drift 추적)
LAB_PROMPT_VERSION = "v4.0"
