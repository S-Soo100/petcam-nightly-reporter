# 윈도우 분석 워커 (MVP-0 walking skeleton) 구현 계획

> **구현 방식 (CAOF):** 이 계획을 task 단위로 구현한다. Standard 트랙 → 메인이 직접 구현(분석→구현 순서 유지). Steps는 체크박스(`- [ ]`)로 추적.
> **작성:** 2026-07-06 · **작업 레포:** `~/petcam-nightly-reporter` · **상위 설계:** `specs/architecture.md` (§4 야간분할 · §5 데이터흐름 · §10 구현단계 · §10.1 indexer)

**Goal:** mac-runner 스모크 골격을 확장해서, terra `motion_clips`의 최근 1~2시간 윈도우 clip을 분석/분류하고 **활동량·활동 시간대 요약을 Slack으로 보내는** 워커를 만든다.

**Architecture:** smoke.py의 4연결점(Supabase·`claude -p`·Slack·`.env`/launchd)을 그대로 계승하고, 그 사이에 "실제 clip 처리"(motion_clips 조회 → R2 다운 → 적응형 프레임 → claude 분류 → 윈도우 활동 집계)를 끼운다. `behavior_logs` write-back은 하지 않는다(MVP-0 스코프 Out, architecture §11). 리포트 뼈대 = **활동량(moving 분) + 활동 시간대 분포 + 탈피**. 미세행동(eat/drink)은 "관찰됨" 수준으로만, 배변은 제외(defecating 클래스 폐기, v4.0).

**Tech Stack:** Python 3.12 · uv · supabase-py(PostgREST) · boto3(R2 = S3 호환) · opencv-python(프레임) · `claude` CLI(구독 headless) · Slack Incoming Webhook · launchd.

**왜 walking skeleton인가:** 라벨링 로직 정밀도보다 **연결점이 다 살아있나**를 먼저 검증한다(스모크 정신 계승). W0→W5로 한 점씩 살리며, 각 단계가 그 자체로 Slack에 뭔가 띄우는 걸 목표로 한다. "인프라 단절 vs 로직 버그"를 항상 분리 진단.

---

## ⚠️ 착수 전 전제 (대화로 확정된 결정)

| 결정 | 값 | 근거 |
|---|---|---|
| 자동화 첫 타겟 | nightly 아침 리포트 | 사용자 선택 2026-07-06 |
| claude 한도 | **구독 그대로 1박 실측 후 분리 결정** | 조기최적화 회피. nightly는 배치라 폴링(288회/일)과 규모 다름 |
| 리포트 뼈대 | 활동량 + 활동 시간대 (+ 탈피) | VLM이 잘하는 것. eat/drink는 천장, 배변은 클래스 폐기 |
| 윈도우 크기 | 2시간 (설정값 `WINDOW_HOURS`) | 사용자 "1~2시간별" |
| clip 입력 | terra `public.motion_clips` (camera_clips 아님) | architecture §10.1, camera_clips는 레거시(06-17 이후 0건) |
| 작업 위치 | 개발·검증 = 맥북 / 상시가동 = 맥미니 | mac-runner 흐름 계승 |

**레포 경계 (CLAUDE.md 룰):**
- 작업 = 이 레포(`petcam-nightly-reporter`). petcam-lab 파일은 건드리지 않는다.
- **복붙 = mac-runner** (smoke 4연결점 + install-launchd.sh).
- **레시피 = petcam-lab에서 "박제/이식"** (import 불가 — 별도 레포). 어느 lab 버전을 이식했는지 리포트 메타·주석에 기록(drift 방지, architecture §6).

---

## File Structure

```
petcam-nightly-reporter/
├── pyproject.toml              # uv, 의존성
├── .env.example / .env         # SUPABASE_* · SLACK_WEBHOOK_URL · R2_*
├── install-launchd.sh          # mac-runner 복붙 + 수정 (W5)
├── smoke.py                    # mac-runner 이식 + motion_clips 핑 (W0)
├── reporter/
│   ├── __init__.py
│   ├── config.py               # .env 로드 + 상수(WINDOW_HOURS 등)
│   ├── timewin.py              # 윈도우 시간 경계 계산 (순수 · 유닛테스트)
│   ├── indexer.py              # motion_clips 윈도우 조회 (W1)
│   ├── r2.py                   # R2 client + clip 다운로드 (W2)
│   ├── frames.py               # 적응형 프레임 추출 (lab 이식, W2)
│   ├── classify.py             # claude -p 분류 (W3)
│   ├── summarize.py            # 윈도우 활동 집계 (순수 · 유닛테스트, W4)
│   ├── slack.py                # webhook 전송
│   ├── worker.py               # 오케스트레이션 main (W4)
│   └── prompts/
│       └── system.v4.0.md      # build_system_prompt 출력 박제 (lab에서, W3)
└── tests/
    ├── test_timewin.py         # 순수 로직 (W1)
    └── test_summarize.py       # 순수 로직 (W4)
```

**테스트 전략:** 외부 연결(Supabase·R2·`claude`·Slack)은 유닛테스트하지 않고 **walking skeleton 수동 검증**으로 생사 확인(donts/python §13 — 실서비스 의존 테스트 분리). 순수 로직(`timewin`·`summarize`)만 pytest TDD.

---

## Task 0 (W0): 레포 부트스트랩 + 스모크 이식

**Files:**
- Create: `pyproject.toml`, `.env.example`, `reporter/__init__.py`, `reporter/config.py`, `smoke.py`

- [ ] **Step 1: `pyproject.toml` 작성**

```toml
[project]
name = "petcam-nightly-reporter"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.27",
    "python-dotenv>=1.0",
    "supabase>=2.7",
    "boto3>=1.34",
    "opencv-python>=4.10",
]

[dependency-groups]
dev = ["pytest>=8.0"]
```

- [ ] **Step 2: `.env.example` 작성** (더미 값만 — 비밀값 커밋 금지, donts §11)

```bash
# Supabase (mac-runner .env 값 복사)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
# Slack Incoming Webhook (mac-runner .env 값 복사)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
# Cloudflare R2 (petcam-lab .env 값 복사)
R2_ENDPOINT=https://your-account.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=your-access-key
R2_SECRET_ACCESS_KEY=your-secret-key
R2_BUCKET=petcam-clips
# 워커 설정
WINDOW_HOURS=2
```

- [ ] **Step 3: `reporter/config.py` — .env 로드 + 상수**

```python
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
```

- [ ] **Step 4: `smoke.py` 이식 — mac-runner 복붙 후 핑을 `motion_clips`로 교체**

mac-runner `smoke.py`를 그대로 복사하되 `ping_supabase()`의 테이블만 교체(camera_clips는 레거시라 이 레포 맥락에 안 맞음):

```python
resp = httpx.get(
    f"{url}/rest/v1/motion_clips",           # camera_clips → motion_clips
    params={"select": "id", "limit": 1},
    headers={"apikey": key, "Authorization": f"Bearer {key}"},
    timeout=10,
)
```

- [ ] **Step 5: `uv sync` + 스모크 실행 → Slack ✅ 확인**

Run: `cd ~/petcam-nightly-reporter && uv sync && cp .env.example .env` (그 뒤 실제 값 채움) `&& uv run python smoke.py`
Expected: 콘솔·Slack에 `✅ supabase · ✅ claude · HH:MM KST`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .env.example reporter/ smoke.py
git commit -m "feat: 레포 부트스트랩 + mac-runner 스모크 이식 (motion_clips 핑)"
```

---

## Task 1 (W1): indexer — 윈도우 clip 조회 → Slack

**Files:**
- Create: `reporter/timewin.py`, `reporter/indexer.py`, `tests/test_timewin.py`

- [ ] **Step 1: `tests/test_timewin.py` — 윈도우 경계 실패 테스트**

윈도우 = "지금(KST) 기준 최근 N시간"을 UTC `[start, end)`로. motion_clips.started_at이 UTC라 UTC로 비교해야 함.

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from reporter.timewin import window_bounds

def test_window_bounds_2h():
    now = datetime(2026, 7, 6, 10, 30, tzinfo=ZoneInfo("Asia/Seoul"))  # 10:30 KST
    start, end = window_bounds(now, hours=2)
    # end = now(UTC) = 01:30 UTC, start = 23:30 UTC 전날
    assert end == datetime(2026, 7, 6, 1, 30, tzinfo=timezone.utc)
    assert start == datetime(2026, 7, 5, 23, 30, tzinfo=timezone.utc)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_timewin.py -v`
Expected: FAIL — `ModuleNotFoundError: reporter.timewin`

- [ ] **Step 3: `reporter/timewin.py` 구현**

```python
"""윈도우 시간 경계. motion_clips.started_at = SNTP UTC 라 UTC 로 계산."""
from datetime import datetime, timedelta, timezone

def window_bounds(now: datetime, hours: float) -> tuple[datetime, datetime]:
    """now(tz-aware) 기준 최근 `hours` 윈도우를 UTC [start, end) 로 반환."""
    end = now.astimezone(timezone.utc)
    start = end - timedelta(hours=hours)
    return start, end
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_timewin.py -v`
Expected: PASS

- [ ] **Step 5: `reporter/indexer.py` — motion_clips 윈도우 조회**

architecture §10.1 쿼리를 supabase-py로. `ClipMeta`는 dataclass(frozen — donts/vlm §3 immutable).

```python
"""terra motion_clips 에서 윈도우 clip 조회 (B방식 — started_at 시간 인덱스)."""
from dataclasses import dataclass
from datetime import datetime
from supabase import create_client
from reporter import config

@dataclass(frozen=True, slots=True)
class ClipMeta:
    id: str
    camera_id: str
    started_at: str
    duration_sec: float
    r2_key: str
    motion_score: float

def list_clips_for_window(start: datetime, end: datetime) -> list[ClipMeta]:
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    rows = (
        sb.table("motion_clips")
        .select("id, camera_id, started_at, duration_sec, r2_key, motion_score")
        .gte("started_at", start.isoformat())
        .lt("started_at", end.isoformat())
        .order("started_at")
        .execute()
        .data
    )
    # r2_key IS NOT NULL 필터 불필요 — terra DB-last 라 row=영상 존재 (architecture §10.1)
    return [ClipMeta(**r) for r in rows]
```

- [ ] **Step 6: 수동 검증 — 최근 2h clip 개수를 Slack으로**

`smoke.py`에 임시 블록 추가(또는 `python -c`)해서 실행:

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from reporter import config, indexer, slack  # slack 은 W0 smoke 함수 재사용
from reporter.timewin import window_bounds

start, end = window_bounds(datetime.now(ZoneInfo("Asia/Seoul")), config.WINDOW_HOURS)
clips = indexer.list_clips_for_window(start, end)
slack.post_slack(f"🔎 최근 {config.WINDOW_HOURS}h clip {len(clips)}개 ({start:%H:%M}~{end:%H:%M} UTC)")
```

Expected: Slack에 clip 개수. (밤이면 수십 개, 낮이면 0~몇 개 — 데이터 유입은 실측됨: 7/4 802건/일)
> ⚠️ `post_slack`을 재사용하려면 W0 smoke의 3함수를 `reporter/slack.py`로 옮기고 smoke.py가 import하게 리팩터(작은 정리). 이 스텝에서 함께.

- [ ] **Step 7: Commit**

```bash
git add reporter/timewin.py reporter/indexer.py reporter/slack.py tests/test_timewin.py smoke.py
git commit -m "feat: 윈도우 시간경계 + motion_clips indexer + clip 개수 slack"
```

---

## Task 2 (W2): R2 다운로드 + 적응형 프레임 추출 (clip 1개)

**Files:**
- Create: `reporter/r2.py`, `reporter/frames.py`

- [x] **Step 1: `reporter/r2.py` — R2 client + 다운로드 (lab r2_uploader 패턴 이식)**

petcam-lab `backend/r2_uploader.py`의 `get_r2_client()` 패턴 복사(path-style 강제 — R2 SSL 이슈 회피). 다운로드만 필요.

```python
"""R2(S3 호환) clip 다운로드. lab backend/r2_uploader.py 의 client 패턴 이식."""
import os
import boto3
from botocore.config import Config
from pathlib import Path

_client = None

def get_r2_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=Config(s3={"addressing_style": "path"}),  # virtual-host cert 매치 실패 회피
            region_name="auto",
        )
    return _client

def download_clip(r2_key: str, dest: Path) -> Path:
    """motion_clips.r2_key 로 mp4 GET → dest 저장."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    get_r2_client().download_file(os.environ["R2_BUCKET"], r2_key, str(dest))
    return dest
```

- [x] **Step 2: `reporter/frames.py` — 적응형 프레임 추출 (lab `_extract_frames_clip.py` 이식)**

lab의 `extract_adaptive` / `_adaptive_n` / `_enforce_no_upscale` 로직을 CLI 벗기고 함수만 이식. 시그니처는 lab과 동치.

```python
"""적응형 프레임 추출 — lab scripts/_extract_frames_clip.py 이식 (v4.0 기준).
간격기반 장수 clamp(round(dur/interval), lo, hi) + 구간중앙 위치 + no-upscale@1080.
"""
import cv2
from pathlib import Path
from reporter import config

def _adaptive_n(dur: float, interval: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, round(dur / interval)))

def extract_adaptive(video: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        dur = total / fps if fps else 0.0
        n = _adaptive_n(dur, config.FRAME_INTERVAL_SEC, config.FRAME_MIN, config.FRAME_MAX)
        # 구간 N등분 후 각 구간 중앙 프레임 (t=0 편향 버그 회피 — feedback_adaptive_frame_sampling)
        paths = []
        for i in range(n):
            frac = (i + 0.5) / n
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frac * total))
            ok, frame = cap.read()
            if not ok:
                continue
            p = out_dir / f"frame_{i:02d}.jpg"
            _write_no_upscale(frame, p)
            paths.append(p)
        return paths
    finally:
        cap.release()  # donts/python §7 — 반드시 해제

def _write_no_upscale(frame, path: Path) -> None:
    h, w = frame.shape[:2]
    long_edge = max(h, w)
    if long_edge > config.FRAME_LONG_EDGE:  # 다운스케일만, 업스케일 금지
        s = config.FRAME_LONG_EDGE / long_edge
        frame = cv2.resize(frame, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path), frame)
```

> ⚠️ 이식 검증: lab 원본과 프레임 위치 로직이 일치하는지 육안 대조(구간중앙·clamp·no-upscale 3요소). 원본이 갱신되면 여기도 sync — `LAB_PROMPT_VERSION`처럼 버전 주석 유지.

- [x] **Step 3: 수동 검증 — clip 1개 다운 + 추출**

```python
from pathlib import Path
from reporter import indexer, r2, frames
from datetime import datetime
from zoneinfo import ZoneInfo
from reporter.timewin import window_bounds
from reporter import config

clips = indexer.list_clips_for_window(*window_bounds(datetime.now(ZoneInfo("Asia/Seoul")), 24))
c = clips[0]
mp4 = r2.download_clip(c.r2_key, Path(f"/tmp/nr/{c.id}.mp4"))
imgs = frames.extract_adaptive(mp4, Path(f"/tmp/nr/{c.id}"))
print(f"clip {c.id[:8]} dur={c.duration_sec}s → {len(imgs)} frames: {[p.name for p in imgs]}")
```

Expected: mp4 다운로드 성공 + 6~20장 jpg. 육안으로 프레임이 게코를 담고 있나 확인.

- [x] **Step 4: Commit**

```bash
git add reporter/r2.py reporter/frames.py
git commit -m "feat: R2 clip 다운로드 + 적응형 프레임 추출 (lab v4.0 레시피 이식)"
```

---

## Task 3 (W3): claude -p 분류 — 기술 관문 (이미지 입력 방식 확정)

**Files:**
- Create: `reporter/prompts/system.v4.0.md`, `reporter/classify.py`

- [x] **Step 1: v4.0 프롬프트 박제 (손조립 금지 — production 함수 출력 캡처)**

메모리 `run-sot-function-reconstruct`: 프롬프트는 SOT 함수를 직접 실행해 박제. ⚠️ species는 **언더스코어** `crested_gecko`(하이픈 아님 — 파일 못 찾음 함정).

Run (petcam-lab 디렉토리에서):
```bash
cd ~/petcam-lab && PYTHONPATH=. uv run python -c \
"from backend.vlm.prompts import build_system_prompt; print(build_system_prompt('crested_gecko', prompt_version='v4.0'))" \
> ~/petcam-nightly-reporter/reporter/prompts/system.v4.0.md
```
Expected: 7-class(moving/drinking/eating_paste/eating_prey/hand_feeding/shedding/unseen) 프롬프트가 저장됨. 파일 상단에 `<!-- 이식: petcam-lab build_system_prompt('crested_gecko','v4.0') 2026-07-06 -->` 주석 추가.

- [x] **Step 2: 스파이크 — `claude -p`에 로컬 이미지 먹이는 방식 확정**

> **기술 미지수:** headless `claude -p`가 프레임 이미지를 보게 하는 정확한 방식. 후보 3개를 clip 1개로 실측해 **가장 안정적인 것 채택**(이 스텝은 계획의 placeholder가 아니라 명시적 검증 스텝).

- 후보 A (경로 언급 + Read 허용): 프롬프트에 프레임 절대경로를 나열하고 Read 도구 허용
  ```bash
  claude -p "다음 프레임들(시간순)을 보고 게코 행동을 분류해. 프레임: /tmp/nr/<id>/frame_00.jpg /tmp/nr/<id>/frame_01.jpg ..." \
    --allowedTools "Read" --output-format json
  ```
- 후보 B (system prompt 주입): `--append-system-prompt "$(cat reporter/prompts/system.v4.0.md)"` + 후보 A 본문
- 후보 C (몽타주 폴백): 프레임을 1장 그리드로 합쳐 경로 1개만 전달 — 활동량 판정엔 충분(미세행동은 어차피 목표 X). A/B가 불안정하면 폴백.

Expected: 셋 중 하나가 프레임 내용을 반영한 JSON 라벨을 반환. 채택 방식을 Step 3 코드에 반영하고 이 문서에 결과 1줄 기록.

**✅ 스파이크 결과 (2026-07-06):** 후보 **A+B 채택** — 경로 나열 + `--allowedTools Read` + `--add-dir` + `--append-system-prompt-file` + `--model sonnet` + `--output-format json`. claude 가 Read 로 로컬 프레임을 정확히 봄(게코 위치까지 검출), 9프레임 → `{action:moving, conf:0.62}` 정확 파싱. ⚠️ **비용/토큰: clip당 ~12만 토큰**(cache_creation 66K + read 55K, output 2K / API 환산 $0.44). 구독은 청구가 아닌 rate-limit이라 **토큰량이 한도 지표** — 밤 수백 clip = 수천만 토큰/night → **전량 분석 시 5h rolling 한도 초과 위험 큼**. → **W4 재설계 신호**: 활동량·시간대(리포트 뼈대)는 motion_clips DB(started_at 분포·clip 수·duration)만으로 claude 0회 산출 가능, claude 는 행동 종류 태깅(탈피/음수)에만 샘플 투입.

- [x] **Step 3: `reporter/classify.py` — 확정 방식으로 clip 분류**

아래는 후보 A/B 채택 가정 골격(스파이크 결과로 명령어만 확정):

```python
"""프레임 → claude -p → 행동 라벨 JSON. Slack 전송 주체는 worker(분리 진단)."""
import json
import subprocess
from pathlib import Path
from reporter import config

_SYSTEM = (Path(__file__).parent / "prompts" / "system.v4.0.md").read_text()

def classify_clip(frame_paths: list[Path]) -> dict:
    """프레임들을 claude 에 주고 {action, confidence, ...} 반환. 실패 시 {'action':'error'}."""
    listed = " ".join(str(p) for p in frame_paths)
    user = (
        f"다음 프레임들(시간순, {len(frame_paths)}장)을 보고 게코의 대표 행동 1개를 v4.0 "
        f"7-class 로 분류해. JSON {{\"action\":..., \"confidence\":0~1}} 만 출력. 프레임: {listed}"
    )
    try:
        r = subprocess.run(
            ["claude", "-p", user,
             "--append-system-prompt", _SYSTEM,
             "--allowedTools", "Read",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"[classify] FAIL: {e}")
        return {"action": "error", "confidence": 0.0}
    if r.returncode != 0:
        print(f"[classify] rc={r.returncode}: {r.stderr.strip()[:200]}")
        return {"action": "error", "confidence": 0.0}
    return _parse(r.stdout)

def _parse(stdout: str) -> dict:
    """claude --output-format json 은 결과를 감싼 envelope. 그 안 텍스트에서 행동 JSON 추출."""
    try:
        envelope = json.loads(stdout)
        text = envelope.get("result", stdout) if isinstance(envelope, dict) else stdout
    except json.JSONDecodeError:
        text = stdout
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"action": "unseen", "confidence": 0.0}  # 파싱 실패 = 보수적 unseen
```

- [x] **Step 4: 수동 검증 — clip 1개 분류**

W2 Step3에서 뽑은 프레임으로 `classify_clip(imgs)` 실행 → 라벨 확인. moving/shedding 등 v4.0 클래스가 나오는지, error가 아닌지.

- [x] **Step 5: Commit**

```bash
git add reporter/prompts/system.v4.0.md reporter/classify.py
git commit -m "feat: claude -p 프레임 분류 (v4.0 프롬프트 박제 + 이미지 입력 방식 확정)"
```

---

## Task 4 (W4): 윈도우 통합 + 활동 집계 + Slack 요약

> ⚠️ **W3 비용 발견으로 재설계 (2026-07-06):** 원안(전량 claude 분류)은 clip당 ~12만 토큰이라 밤배치 시 구독 한도 초과. **W4a(DB 뼈대·claude 0회) + W4b(claude 샘플)로 분리.**
> - **W4a ✅ (완료):** `summarize_activity(clips)` — 활동량(clip 수·duration)·활동시간대(started_at→KST 분포)를 라벨 없이 DB 로 산출. `worker.py` 뼈대(조회→집계→slack). 실측: 21-23시 윈도우 47clip/24.3분/22시 집중, **0 비용**.
> - **W4b ✅ (완료):** `motion_score` 상위 N개(`SAMPLE_TOP_N`, 기본 5) clip만 `classify_clip` → `summarize_behaviors`(탈피·음수·급여 태깅) → `_format` slack 통합. 검증 N=2: 52clip 뼈대 + top2 claude→moving, 격리 except OK. N 은 밤 실측 후 조정.
> - **한도 전략:** W5 launchd 를 22/00/02/04시 분산 → 각 배치가 직전 2h 만 처리 + 5h rolling 부분회복 + 새벽 미사용 독점(메모리 `claude-subscription-quota-shared`). 정 안되면 전용 계정.
>
> 아래 원안 스텝(Step 1~7)은 **W4b 참조용**. 실제 W4a 는 위 구조로 구현됨(summarize 는 action 무관 activity 집계, worker 는 claude 없는 뼈대).

**Files:**
- Create: `reporter/summarize.py`, `reporter/worker.py`, `tests/test_summarize.py`

- [ ] **Step 1: `tests/test_summarize.py` — 활동 집계 실패 테스트**

리포트 뼈대 = 활동량(moving 클립 비율·추정 분) + 활동 시간대 + 탈피 관찰. 순수 함수라 TDD.

```python
from reporter.summarize import summarize_window

def test_summarize_activity():
    items = [
        {"started_at": "2026-07-06T13:00:00+00:00", "duration_sec": 60, "action": "moving"},
        {"started_at": "2026-07-06T13:20:00+00:00", "duration_sec": 60, "action": "moving"},
        {"started_at": "2026-07-06T14:00:00+00:00", "duration_sec": 60, "action": "shedding"},
        {"started_at": "2026-07-06T14:10:00+00:00", "duration_sec": 60, "action": "unseen"},
    ]
    s = summarize_window(items)
    assert s["clip_count"] == 4
    assert s["active_clips"] == 2               # moving 2건
    assert s["active_minutes"] == 2.0           # 60s*2 = 2분
    assert s["shed_observed"] is True
    assert s["peak_hour_kst"] == 22             # 13:00 UTC = 22:00 KST
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_summarize.py -v`
Expected: FAIL — `ModuleNotFoundError: reporter.summarize`

- [ ] **Step 3: `reporter/summarize.py` 구현**

```python
"""윈도우 clip 라벨들 → 활동 요약. 활동량·시간대 중심(리포트 뼈대). 배변 제외(v4.0)."""
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

_ACTIVE = {"moving", "drinking", "eating_paste", "eating_prey", "hand_feeding"}  # 은신/탈피 제외

def summarize_window(items: list[dict]) -> dict:
    """items = [{started_at, duration_sec, action}]. 활동량/시간대/탈피 집계."""
    active = [it for it in items if it["action"] in _ACTIVE]
    active_sec = sum(it["duration_sec"] for it in active)
    hours = Counter(
        datetime.fromisoformat(it["started_at"]).astimezone(ZoneInfo("Asia/Seoul")).hour
        for it in active
    )
    return {
        "clip_count": len(items),
        "active_clips": len(active),
        "active_minutes": round(active_sec / 60, 1),
        "shed_observed": any(it["action"] == "shedding" for it in items),
        "peak_hour_kst": hours.most_common(1)[0][0] if hours else None,
        "actions": dict(Counter(it["action"] for it in items)),
    }
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_summarize.py -v`
Expected: PASS

- [ ] **Step 5: `reporter/worker.py` — 오케스트레이션 main**

```python
"""윈도우 워커 main: 조회 → 다운 → 프레임 → 분류 → 집계 → Slack. 돌고 죽는다(launchd 재실행)."""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from reporter import config, indexer, r2, frames, classify, summarize, slack
from reporter.timewin import window_bounds

def run() -> int:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    start, end = window_bounds(now, config.WINDOW_HOURS)
    clips = indexer.list_clips_for_window(start, end)
    if not clips:
        slack.post_slack(f"🦎 최근 {config.WINDOW_HOURS}h: 활동 클립 없음 ({now:%m/%d %H:%M} KST)")
        return 0

    items = []
    with tempfile.TemporaryDirectory() as tmp:
        for c in clips:
            try:
                mp4 = r2.download_clip(c.r2_key, Path(tmp) / f"{c.id}.mp4")
                imgs = frames.extract_adaptive(mp4, Path(tmp) / c.id)
                label = classify.classify_clip(imgs)
            except Exception as e:  # 클립 1개 실패가 윈도우 전체를 막지 않게 (격리)
                print(f"[worker] clip {c.id[:8]} skip: {e}", file=sys.stderr)
                label = {"action": "error"}
            items.append({"started_at": c.started_at, "duration_sec": c.duration_sec, **label})

    s = summarize.summarize_window(items)
    slack.post_slack(_format(s, now))
    return 0

def _format(s: dict, now: datetime) -> str:
    shed = " · 🧬탈피 관찰" if s["shed_observed"] else ""
    peak = f"{s['peak_hour_kst']}시경 집중" if s["peak_hour_kst"] is not None else "활동 미미"
    return (
        f"🦎 최근 {config.WINDOW_HOURS}h 활동 요약 ({now:%m/%d %H:%M} KST)\n"
        f"· 클립 {s['clip_count']}개 중 활동 {s['active_clips']}개 (~{s['active_minutes']}분)\n"
        f"· {peak}{shed}\n"
        f"· 관찰 행동: {s['actions']}"
    )

if __name__ == "__main__":
    sys.exit(run())
```

- [ ] **Step 6: 수동 1회 실행 — 실제 윈도우 활동 요약 Slack**

Run: `cd ~/petcam-nightly-reporter && uv run python -m reporter.worker`
Expected: Slack에 활동 요약 카드. (밤 윈도우에 돌리면 실제 활동 반영)
> 📌 **한도 실측 시작점:** 이 실행의 `claude` 호출 수 = 윈도우 clip 수. 밤 윈도우(수십~수백 clip)로 1박 돌려 실제 호출량·구독 한도 소모를 측정 → 분리 여부 결정(전제 표). 필요 시 clip 배치 묶기/motion_score 상위 N개 샘플링으로 조절.

- [ ] **Step 7: Commit**

```bash
git add reporter/summarize.py reporter/worker.py tests/test_summarize.py
git commit -m "feat: 윈도우 워커 통합 — 활동량/시간대 집계 + slack 요약"
```

---

## Task 5 (W5): launchd 스케줄 (맥미니 상시 가동)

**Files:**
- Create: `install-launchd.sh` (mac-runner 복붙 + 수정), `README.md`

- [ ] **Step 1: `install-launchd.sh` — mac-runner 복붙 + 타겟/PATH 수정**

mac-runner `install-launchd.sh` 복사 후:
- 실행 타겟: `smoke.py` → `-m reporter.worker`
- **PATH 이중 확보**(메모리 `cron-launchd-keychain` PATH 함정): `uv`(`~/.local/bin`)와 `claude`(brew `/opt/homebrew/bin`)가 다른 bin일 수 있음 → plist `PATH`에 `command -v uv`/`command -v claude` 둘 다 반영.
- 주기: 스모크·검증은 `StartInterval`(N초). **야간 한정 운영 전환 시** `StartCalendarInterval` 배열(00·02·04·06시)로 — 사용자와 야간 스케줄 확정 후.

- [ ] **Step 2: `README.md` — 맥미니 핸드오프 절차**

mac-runner Phase 0 절차 계승:
```
git clone → uv sync → .env (맥북 값 복사: SUPABASE_*·SLACK_WEBHOOK_URL·R2_*)
→ claude -p "say hi" 로그인 확인(설치≠로그인) → uv run python -m reporter.worker 수동검증
→ ./install-launchd.sh → tail 로그 → 필요 시 sudo reboot 재부팅 생존 확인
```

- [ ] **Step 3: (맥미니) 설치 + 수동 트리거 1회 검증**

Run (맥미니): `./install-launchd.sh && uv run python -m reporter.worker`
Expected: Slack 활동 요약 도착.
> ⚠️ **claude 구독 한도 공유**(메모리 `claude-subscription-quota-shared`): 맥미니 워커가 본인 claude 작업과 같은 구독 한도 사용. W4 실측 데이터로 야간 주기·분리 결정 후 상시 가동.

- [ ] **Step 4: Commit**

```bash
git add install-launchd.sh README.md
git commit -m "feat: launchd 스케줄 + 맥미니 핸드오프 절차 (mac-runner 패턴 계승)"
```

---

## 스코프 (In / Out)

**In (이 계획):** motion_clips 윈도우 조회 · R2 다운 · 적응형 프레임 · claude 분류 · 활동량/시간대 집계 · Slack 요약 · launchd. 리포트 뼈대 = 활동량 + 시간대 + 탈피.

**Out (다음):** 06시 야간 종합 1장(architecture §4 — 지금은 윈도우별 단발 요약까지) · `behavior_logs` write-back · 미세행동 정밀 카운트(eat/drink 천장) · 배변(클래스 폐기) · Gate prelabel 재활용(§7) · 다정한 페르소나 브리핑 문체(SOT §4-2 — 뼈대 검증 후 프롬프트만 교체) · 클라우드 자동화(MVP-1).

---

## Self-Review

**1. Spec coverage (architecture 대비):**
- §4 야간분할 → Task 4·5 (윈도우 워커 + launchd). 06시 종합은 Out으로 명시(다음 단계).
- §5 데이터흐름(R2→motion scan→프레임→claude) → Task 2·3·4. ※ MVP-0은 `motion_scan`(clip 내부 event 재분할)을 생략하고 clip=분석단위로 단순화 — motion_clips가 이미 모션 트리거라 walking skeleton엔 충분. event 세분화는 Out.
- §6 lab 레시피 소비(프레임·프롬프트) → Task 2·3 이식 + 버전 주석.
- §10.1 indexer(motion_clips B쿼리) → Task 1.
- 전제 표(한도 실측/활동량 뼈대/윈도우 2h) → 각 Task 노트에 반영.

**2. Placeholder scan:** W3 Step2 "스파이크"는 미지수를 명시적으로 검증하는 실제 스텝(후보 3개 명령어 제시) — placeholder 아님. "add error handling" 류 없음. worker의 clip 격리 except는 이유 주석 포함.

**3. Type consistency:**
- `ClipMeta`(indexer) 필드 = motion_clips 실측 컬럼(id/camera_id/started_at/duration_sec/r2_key/motion_score) ✅ 일관.
- `classify_clip` 반환 `{action, confidence}` → worker에서 `**label`로 병합 → `summarize_window`가 `it["action"]` 읽음 ✅.
- `extract_adaptive(video, out_dir)` 시그니처 = worker 호출부 일치 ✅.
- `window_bounds(now, hours)` = indexer·worker 호출부 일치 ✅.
- species = `crested_gecko`(언더스코어) — W3 Step1에 함정 명시 ✅.

**미해결(구현 중 확정):** ① W3 claude 이미지 입력 방식(스파이크). ② `--output-format json` envelope 실제 구조(`_parse`가 방어적으로 처리). ③ 밤 윈도우 실제 호출량(W4 실측).
