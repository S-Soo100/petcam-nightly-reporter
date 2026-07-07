"""워커 전역 설정. .env 는 이 패키지 부모(레포 루트)에서 로드."""
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Cloudflare R2 (S3 호환) — clip 다운로드. R2_ACCOUNT_ID 는 endpoint 에 포함돼 client 는 안 씀.
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")

WINDOW_HOURS = float(os.environ.get("WINDOW_HOURS", "0.5"))  # 30분 상황판(준실시간). 2h→0.5h

# 적응형 프레임 추출 (lab 레시피 v4.0 기준 — feedback_adaptive_frame_sampling)
FRAME_INTERVAL_SEC = 3.5
FRAME_MIN = 6
FRAME_MAX = 20
FRAME_LONG_EDGE = 1080  # no-upscale cap

# W4b 샘플 태깅: motion_score 상위 N개 clip 만 claude 분류(한도 통제). 30분 상황판은 창당 1개
# (밤 top-1)로 시작 — nightly 4회×5 와 claude 호출량 동급. 오늘밤 로그 여유 보고 2 로 상향 검토.
SAMPLE_TOP_N = int(os.environ.get("SAMPLE_TOP_N", "1"))

# 하이라이트 자동 등록(auto-register): informative 샘플을 camera_clips 미러 + behavior_logs(vlm
# 후보)로 편입 → 앱 추론뷰·라벨링 큐에 노출. owner/pet 은 단일 오너 펫캠 상수(lab recipe
# register_motion_candidates.py 와 동일). 문제 시 REGISTER_HIGHLIGHTS=0 으로 즉시 차단.
REGISTER_HIGHLIGHTS = os.environ.get("REGISTER_HIGHLIGHTS", "1") == "1"
REGISTER_OWNER_USER_ID = os.environ.get("REGISTER_OWNER_USER_ID", "380d97fd-cb83-4490-ac26-cf691b32614f")
REGISTER_PET_ID = os.environ.get("REGISTER_PET_ID", "55518f35-b251-4ed7-962f-b65611d63223")
REGISTER_VLM_MODEL = "claude-sonnet-4-6"  # 실제 모델(--model sonnet). nightly 출처는 behavior_logs.notes
REGISTER_SKIP_ACTIONS = frozenset({"moving", "error", "unseen"})  # 흔함/분류실패/게코부재 제외

# 이식한 lab 자산 버전 (drift 추적)
LAB_PROMPT_VERSION = "v4.0"
