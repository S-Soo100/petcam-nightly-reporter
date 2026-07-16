#!/usr/bin/env bash
# Rolling 과거 야간 VLM backfill worker. 시작일 2026-07-07 고정, 종료일 없음 — 완전히 종료된
# 과거 야간을 자동 발견해 시간당 최대 30개(일 최대 600개)만 처리한다. backlog 가 없으면
# Claude/Slack 0회. 결과는 shadow VLM job 에만 기록하며 앱 하이라이트·활동시간·GT 는 변경하지 않는다.
set -euo pipefail

LABEL="com.petcam.vlm-historical-backfill"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="$(id -un)"
EXPECTED_HOST="${VLM_EXPECTED_HOST:-}"
UV_BIN="$(command -v uv || true)"
CLAUDE_BIN="$(command -v claude || true)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -z "$UV_BIN" ] || [ -z "$CLAUDE_BIN" ]; then
  echo "uv 또는 claude CLI를 PATH에서 찾지 못해 설치를 중단함" >&2
  exit 1
fi

# H2 host guard: 검증된 Mac mini hostname 을 명시해야 한다. 현재 hostname 을 자동 승인하지 않는다.
[ -n "$EXPECTED_HOST" ] || { echo "VLM_EXPECTED_HOST 필수(검증된 Mac mini hostname)" >&2; exit 1; }
ACTUAL_HOST="$("$UV_BIN" run python -c 'import socket;print(socket.gethostname())')"
[ "$ACTUAL_HOST" = "$EXPECTED_HOST" ] || { echo "hostname 불일치 — expected host 가 아닌 곳에 설치 중단" >&2; exit 1; }

# 계정 원문은 이메일을 포함할 수 있으므로 설치 로그에 절대 출력하지 않는다.
if ! "$UV_BIN" run python -c 'from reporter.claude_cli_analyzer import check_cli_auth; check_cli_auth()' >/dev/null 2>&1; then
  echo "Claude CLI 구독 인증을 확인하지 못해 설치를 중단함" >&2
  exit 1
fi

CHECKPOINT="$("$UV_BIN" run python -c 'from reporter.config import GATE_CHECKPOINT_PATH; print(GATE_CHECKPOINT_PATH)')"
if [ ! -f "$CHECKPOINT" ]; then
  echo "Gate 체크포인트가 없어 설치를 중단함" >&2
  exit 1
fi

LAUNCHD_PATH="$(dirname "$UV_BIN"):$(dirname "$CLAUDE_BIN"):/usr/bin:/bin"
# rolling: 24시간 매시간 :35 calendar. 코드 guard(rolling_backfill_allowed_now)가 정규 VLM
# (22/00/02/04) ±30분 실행을 fail-closed 로 차단하므로 21:35/23:35/01:35/03:35 는 no-op 된다.
CAL=""
for HOUR in $(seq 0 23); do
  CAL="$CAL<dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>35</integer></dict>"
done
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>reporter.vlm_backfill_worker</string></array>
<key>WorkingDirectory</key><string>$REPO_DIR</string>
<key>StartCalendarInterval</key><array>$CAL</array>
<key>StandardOutPath</key><string>/tmp/vlm-historical-backfill.log</string>
<key>StandardErrorPath</key><string>/tmp/vlm-historical-backfill.log</string>
<key>EnvironmentVariables</key><dict>
<key>PATH</key><string>$LAUNCHD_PATH</string>
<key>HOME</key><string>$HOME</string>
<key>USER</key><string>$RUN_USER</string>
<key>LOGNAME</key><string>$RUN_USER</string>
<key>VLM_PROVIDER</key><string>claude_cli_batch</string>
<key>VLM_EXPECTED_HOST</key><string>$EXPECTED_HOST</string>
<key>REGISTER_HIGHLIGHTS</key><string>0</string>
<key>ANTHROPIC_MODEL_EXACT</key><string>claude-sonnet-5</string>
<key>PYTHONUNBUFFERED</key><string>1</string>
</dict>
</dict></plist>
PLISTEOF

if ! plutil -lint "$PLIST" >/dev/null; then
  echo "plist 문법 오류로 설치를 중단함" >&2
  exit 1
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "installed $LABEL provider=claude_cli_batch model=claude-sonnet-5"
echo "24시간 매시간 :35 실행(rolling). 정규 VLM ±30분은 코드 guard 로 no-op. backlog 없으면 Claude 0"
echo "log: /tmp/vlm-historical-backfill.log"
echo "stop: launchctl bootout gui/$(id -u)/$LABEL"
