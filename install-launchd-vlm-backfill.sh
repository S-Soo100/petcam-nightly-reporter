#!/usr/bin/env bash
# 2026-07-07~14 과거 야간 영상 240개를 Claude Code 구독으로 시간당 30개씩 처리하는 임시 worker.
# 결과는 shadow VLM job에만 기록하며 앱 하이라이트·활동시간·GT는 변경하지 않는다.
set -euo pipefail

LABEL="com.petcam.vlm-historical-backfill"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="$(id -un)"
UV_BIN="$(command -v uv || true)"
CLAUDE_BIN="$(command -v claude || true)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -z "$UV_BIN" ] || [ -z "$CLAUDE_BIN" ]; then
  echo "uv 또는 claude CLI를 PATH에서 찾지 못해 설치를 중단함" >&2
  exit 1
fi

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
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>reporter.vlm_backfill_worker</string></array>
<key>WorkingDirectory</key><string>$REPO_DIR</string>
<key>RunAtLoad</key><true/>
<key>StartInterval</key><integer>3600</integer>
<key>StandardOutPath</key><string>/tmp/vlm-historical-backfill.log</string>
<key>StandardErrorPath</key><string>/tmp/vlm-historical-backfill.log</string>
<key>EnvironmentVariables</key><dict>
<key>PATH</key><string>$LAUNCHD_PATH</string>
<key>HOME</key><string>$HOME</string>
<key>USER</key><string>$RUN_USER</string>
<key>LOGNAME</key><string>$RUN_USER</string>
<key>VLM_PROVIDER</key><string>claude_cli_batch</string>
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
echo "RunAtLoad 1회 후 1시간마다 실행하며, 240개 완료 시 자동 no-op"
echo "log: /tmp/vlm-historical-backfill.log"
echo "stop: launchctl bootout gui/$(id -u)/$LABEL"
