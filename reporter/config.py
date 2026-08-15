"""워커 전역 설정. .env 는 이 패키지 부모(레포 루트)에서 로드."""
import os
from decimal import Decimal
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

# 밤(20~06시 KST)만 분석 — SAMPLE_TOP_N↑ 하면 낮 A카메라 오탐이 claude 한도를 갉아먹어 밤 예산이
# 준다. 낮 창은 worker 가 즉시 종료(claude 0). NIGHT_ONLY=0 으로 24h 복귀. (2026-07-07)
NIGHT_ONLY = os.environ.get("NIGHT_ONLY", "1") == "1"

# 하이라이트 자동 등록(auto-register): informative 샘플을 camera_clips 미러 + behavior_logs(vlm
# 후보)로 편입 → 앱 추론뷰·라벨링 큐에 노출. owner/pet 은 단일 오너 펫캠 상수(lab recipe
# register_motion_candidates.py 와 동일). 문제 시 REGISTER_HIGHLIGHTS=0 으로 즉시 차단.
REGISTER_HIGHLIGHTS = os.environ.get("REGISTER_HIGHLIGHTS", "1") == "1"
REGISTER_OWNER_USER_ID = os.environ.get("REGISTER_OWNER_USER_ID", "380d97fd-cb83-4490-ac26-cf691b32614f")
REGISTER_PET_ID = os.environ.get("REGISTER_PET_ID", "55518f35-b251-4ed7-962f-b65611d63223")
REGISTER_VLM_MODEL = "claude-sonnet-4-6"  # 실제 모델(--model sonnet). nightly 출처는 behavior_logs.notes
# shedding 추가: 이 캠 개체(화이트 많은 트라이 익스트림 할리퀸)는 흰 체색을 밤 IR 서 허물로 상시
# 오탐(실제 밤 탈피 영상 0 = 전부 오탐, 누적 22개 다 moving). auto-register 큐 오염 방지.
# 실제 밤 탈피 영상 확보 시 REGISTER_SKIP_ACTIONS env 에서 shedding 빼면 복귀. (2026-07-08)
REGISTER_SKIP_ACTIONS = frozenset(
    os.environ.get("REGISTER_SKIP_ACTIONS", "moving,error,unseen,shedding").split(",")
)

# 이식한 lab 자산 버전 (drift 추적)
LAB_PROMPT_VERSION = "v4.0"

# --- 활동필터 worker (activity_worker) — 기존 상황판 worker 와 완전 독립, Claude/VLM 0회 ---
# Gate 체크포인트: REPO_ROOT 부모(../) 기준 조립 = 절대경로 하드코딩 아님. .env 로 override.
GATE_CHECKPOINT_PATH = os.environ.get(
    "GATE_CHECKPOINT_PATH",
    str(REPO_ROOT.parent / "myPythonProjects/gecko-vision-gate/runs/gecko_v2/checkpoint_best_ema.pth"),
)
# threshold 0.25 는 recall 우선 후보값 — Phase 3 사람 GT 로 확정 전까지 정식값 아님(지시문 §207).
GATE_THRESHOLD = float(os.environ.get("GATE_THRESHOLD", "0.25"))
ACTIVITY_POLICY_VERSION = os.environ.get("ACTIVITY_POLICY_VERSION", "activity-v0")
ACTIVITY_WINDOW_HOURS = float(os.environ.get("ACTIVITY_WINDOW_HOURS", "24"))
ACTIVITY_BATCH_LIMIT = int(os.environ.get("ACTIVITY_BATCH_LIMIT", "200"))
# activity worker fail-closed host guard(§5.1). 검증된 Mac mini hostname 을 명시해야 worker 가
# lock/DB/R2/detector/Slack 을 건드린다. 미설정/불일치면 side effect 이전에 nonzero 종료.
# installer 가 현재 hostname 을 자동 승인하지 못하도록 expected 는 배포자가 명시한 값을 쓴다.
ACTIVITY_EXPECTED_HOST = os.environ.get("ACTIVITY_EXPECTED_HOST", "")

# --- 전 영상 Python evidence worker (python_evidence_worker) — activity worker 와 완전 독립 ---
# 기본 비활성(false): migration 없는 환경/미승인 host 에서 신규 table query 0 을 보장한다(설계 §11/§203).
# enabled 여야 DB client 생성 전에도 통과. batch limit 은 [1,200] 로 clamp(폭주 방지). expected host 는
# 검증된 Mac mini hostname 을 배포자가 명시해야 하며 현재 hostname 을 자동 승인하지 않는다(§196).
PYTHON_EVIDENCE_ENABLED = os.environ.get("PYTHON_EVIDENCE_ENABLED", "0") == "1"
PYTHON_EVIDENCE_BATCH_LIMIT = min(max(int(os.environ.get("PYTHON_EVIDENCE_BATCH_LIMIT", "30")), 1), 200)
PYTHON_EVIDENCE_EXPECTED_HOST = os.environ.get("PYTHON_EVIDENCE_EXPECTED_HOST", "")
# 전 영상 evidence 전용 Gate threshold (activity 의 GATE_THRESHOLD 를 재사용하지 않는다 — H2). 검증된
# 0.10 을 기본/배포값으로 고정. worker 가 fail-closed 로 (0,1] 범위를 확인하고 아니면 실행을 거부한다.
PYTHON_EVIDENCE_GATE_THRESHOLD = float(os.environ.get("PYTHON_EVIDENCE_GATE_THRESHOLD", "0.10"))
# sparse Gate 최소 프레임. 미달이면 prelabel 을 저장하지 않고(불완전 프레임을 gecko absent 로 굳히지 않음)
# terminal decode_insufficient_frames 로 처리한다(설계 §7.1).
PYTHON_EVIDENCE_MIN_FRAMES = int(os.environ.get("PYTHON_EVIDENCE_MIN_FRAMES", "6"))

# --- Gecko Motion Engine production shadow ---
# 기존 Python Evidence와 독립된 직접 교체 worker. 기본 비활성 + exact host guard로 fail-closed.
GME_ENABLED = os.environ.get("GME_ENABLED", "0") == "1"
GME_EXPECTED_HOST = os.environ.get("GME_EXPECTED_HOST", "")
GME_BATCH_LIMIT = min(max(int(os.environ.get("GME_BATCH_LIMIT", "4")), 1), 50)
GME_MAX_LIVE_LAG_SEC = float(os.environ.get("GME_MAX_LIVE_LAG_SEC", "900"))
GME_GATE_THRESHOLD = float(os.environ.get("GME_GATE_THRESHOLD", "0.5"))
GME_ANALYSIS_FPS = min(max(float(os.environ.get("GME_ANALYSIS_FPS", "30")), 1.0), 30.0)
GME_ANCHOR_INTERVAL_SEC = float(os.environ.get("GME_ANCHOR_INTERVAL_SEC", "0.5"))
GME_CHECKPOINT_PATH = os.environ.get("GME_CHECKPOINT_PATH", GATE_CHECKPOINT_PATH)
GME_DETECTOR_BACKEND = os.environ.get("GME_DETECTOR_BACKEND", "rfdetr")
GME_CHECKPOINT_SHA256 = os.environ.get("GME_CHECKPOINT_SHA256", "")
GME_RAW_CONFIDENCE = float(os.environ.get("GME_RAW_CONFIDENCE", "0.001"))
GME_SCORE_THRESHOLD = float(os.environ.get("GME_SCORE_THRESHOLD", "0.20"))
GME_IMAGE_SIZE = int(os.environ.get("GME_IMAGE_SIZE", "960"))
GME_NMS_IOU = float(os.environ.get("GME_NMS_IOU", "0.70"))
GME_MAX_DETECTIONS = int(os.environ.get("GME_MAX_DETECTIONS", "50"))
GME_DEVICE = os.environ.get("GME_DEVICE", "mps")
GME_PERMANENT_PREFIX = "terra-derived/gme/v1/permanent/"
GME_DEBUG_PREFIX = "terra-derived/gme/v1/debug-14d/"

# --- camera_clips 라벨링 격리 제안 worker — 기본은 완전 비활성/쓰기 금지 ---
LABELING_TRIAGE_ENABLED = os.environ.get("LABELING_TRIAGE_ENABLED", "0") == "1"
LABELING_TRIAGE_WRITE_ENABLED = os.environ.get("LABELING_TRIAGE_WRITE_ENABLED", "0") == "1"
LABELING_TRIAGE_POLICY_VERSION = os.environ.get(
    "LABELING_TRIAGE_POLICY_VERSION", "labeling-triage-v1"
)
LABELING_TRIAGE_ACTIVITY_POLICY_VERSION = os.environ.get(
    "LABELING_TRIAGE_ACTIVITY_POLICY_VERSION", "activity-v1"
)
LABELING_TRIAGE_WINDOW_HOURS = float(os.environ.get("LABELING_TRIAGE_WINDOW_HOURS", "168"))
LABELING_TRIAGE_BATCH_LIMIT = int(os.environ.get("LABELING_TRIAGE_BATCH_LIMIT", "30"))
LABELING_TRIAGE_FRAMES = int(os.environ.get("LABELING_TRIAGE_FRAMES", "12"))

VLM_ROUTER_ENABLED = os.environ.get("VLM_ROUTER_ENABLED", "0") == "1"
# 정규 candidate worker fail-closed host guard(§6.1). 검증된 Mac mini hostname 을 명시해야
# enabled worker 가 DB/Claude/Slack 을 건드린다. 미설정이면 enabled 실행은 즉시 nonzero 종료.
VLM_EXPECTED_HOST = os.environ.get("VLM_EXPECTED_HOST", "")
VLM_PROVIDER = os.environ.get("VLM_PROVIDER", "direct_api")
VLM_SELECTOR_VERSION = "budget-router-v1"
VLM_ACTIVITY_POLICY_VERSION = "activity-v1"
VLM_MODEL = os.environ.get("ANTHROPIC_MODEL_EXACT", "claude-sonnet-5")
VLM_PROMPT_VERSION = "v4.0-direct-images"
VLM_SAMPLER_VERSION = "six-768q85-v1"
VLM_MONTHLY_BUDGET_USD = Decimal(os.environ.get("VLM_MONTHLY_BUDGET_USD", "10.00"))
VLM_RESERVED_COST_USD = Decimal(os.environ.get("VLM_RESERVED_COST_USD", "0.10"))
VLM_MAX_PER_CAMERA_WINDOW = 4
VLM_MAX_PER_CAMERA_NIGHT = 16
VLM_MAX_PER_NIGHT = 64

# ── 짧은 영상 장치 오류 격리·보존 worker (short_clip_retention_worker) ──
# 메타데이터 전용 감지 + 7일 보존 후 exact R2 삭제. 감지/쓰기/삭제 독립 switch, 기본 전부 비활성.
# EXPECTED_HOST 는 검증된 Mac mini hostname(자동 승인 금지) — enabled 실행 시 host guard 가 lock/DB/
# R2/Slack 이전에 fail-closed 한다. BATCH_LIMIT [1,200], DELETE_LIMIT [1,30] clamp.
SHORT_CLIP_RETENTION_ENABLED = os.environ.get("SHORT_CLIP_RETENTION_ENABLED", "0") == "1"
SHORT_CLIP_RETENTION_WRITE_ENABLED = (
    os.environ.get("SHORT_CLIP_RETENTION_WRITE_ENABLED", "0") == "1"
)
SHORT_CLIP_RETENTION_DELETE_ENABLED = (
    os.environ.get("SHORT_CLIP_RETENTION_DELETE_ENABLED", "0") == "1"
)
SHORT_CLIP_RETENTION_EXPECTED_HOST = os.environ.get(
    "SHORT_CLIP_RETENTION_EXPECTED_HOST", ""
)
SHORT_CLIP_RETENTION_BATCH_LIMIT = min(
    max(int(os.environ.get("SHORT_CLIP_RETENTION_BATCH_LIMIT", "100")), 1), 200
)
SHORT_CLIP_RETENTION_DELETE_LIMIT = min(
    max(int(os.environ.get("SHORT_CLIP_RETENTION_DELETE_LIMIT", "30")), 1), 30
)
# candidate 후보 임계(초). DB 정책이 정본이지만 worker 가 후보 목록 RPC 에 넘길 상한. 15초 미만.
SHORT_CLIP_RETENTION_CANDIDATE_UNDER_SEC = float(
    os.environ.get("SHORT_CLIP_RETENTION_CANDIDATE_UNDER_SEC", "15")
)
# 일일 Slack 카드 KST 리포트 시각(이 시각 이후 사이클에서만 1회 전송). 기본 09시.
SHORT_CLIP_RETENTION_REPORT_HOUR_KST = int(
    os.environ.get("SHORT_CLIP_RETENTION_REPORT_HOUR_KST", "9")
)
