#!/usr/bin/env python3
"""nightly-reporter Phase 0 walking-skeleton 스모크 (mac-runner 이식).

4개 연결점(스케줄러·Supabase·Claude·Slack)이 다 살아있는지 한 줄로 관통 검증한다.
실제 윈도우 분석 로직(W1~)을 붙이기 전에 "인프라 단절 vs 로직 버그" 변수를 미리 제거.

mac-runner smoke.py 대비 차이: Supabase 핑 테이블 camera_clips → motion_clips
(camera_clips 는 이 레포 맥락에서 레거시, 실제 입력은 terra motion_clips).
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

REQUIRED = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SLACK_WEBHOOK_URL")


def ping_supabase() -> bool:
    """motion_clips 1건 select 핑 — 연결·인증·테이블 생사 확인.

    Supabase 는 PostgREST 라 테이블이 곧 REST 엔드포인트(/rest/v1/<table>).
    service_role 키는 apikey 헤더 + Bearer 양쪽에 넣는다(PostgREST 관례).
    """
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    try:
        resp = httpx.get(
            f"{url}/rest/v1/motion_clips",
            params={"select": "id", "limit": 1},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as e:
        print(f"[supabase] FAIL: {e}", file=sys.stderr)
        return False


def call_claude() -> str | None:
    """`claude -p` headless 호출 → stdout 텍스트 수신 = Claude 구동 증명. 구독 커버."""
    try:
        result = subprocess.run(
            ["claude", "-p", "Reply with exactly one word: pong"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        # FileNotFoundError = claude 가 PATH 에 없음(launchd PATH 함정 — cron-launchd-keychain).
        print(f"[claude] FAIL: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"[claude] FAIL rc={result.returncode}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return result.stdout.strip()


def post_slack(text: str) -> bool:
    """Slack Incoming Webhook 으로 1줄 전송. 성공 신호 창."""
    try:
        resp = httpx.post(os.environ["SLACK_WEBHOOK_URL"], json={"text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as e:
        print(f"[slack] FAIL: {e}", file=sys.stderr)
        return False


def main() -> int:
    # cwd 와 무관하게 이 스크립트 옆의 .env 로드(launchd cwd 함정 방어 — donts/python §11).
    load_dotenv(Path(__file__).resolve().parent / ".env")

    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        print(f"[config] 누락 환경변수: {', '.join(missing)} — .env 확인", file=sys.stderr)
        return 2

    supabase_ok = ping_supabase()
    claude_out = call_claude()
    claude_ok = claude_out is not None

    now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M")
    line = (
        f"{'✅' if supabase_ok else '❌'} supabase(motion_clips) · "
        f"{'✅' if claude_ok else '❌'} claude · {now} KST [nightly smoke]"
    )

    post_slack(line)
    print(line)
    if claude_out:
        print(f"[claude] stdout: {claude_out!r}", file=sys.stderr)

    return 0 if (supabase_ok and claude_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
