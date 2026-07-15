#!/usr/bin/env bash
# 2026-07-15 20:30 KST 1회성 finalizer LaunchAgent 설치.
# 최종 판정·보고·SOT 정리는 Claude Code(구독 인증)가 수행한다 — 단순 python cron이 아니다.
set -euo pipefail

LABEL="com.petcam.vlm-backfill-finalizer"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="$(id -un)"
UV_BIN="$(command -v uv || true)"
CLAUDE_BIN="$(command -v claude || true)"
WRAPPER="$REPO_DIR/run-vlm-backfill-finalizer.sh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -z "$UV_BIN" ] || [ -z "$CLAUDE_BIN" ]; then
  echo "uv 또는 claude CLI를 PATH에서 찾지 못해 설치를 중단함" >&2
  exit 1
fi
if [ ! -f "$WRAPPER" ]; then
  echo "wrapper 스크립트가 없어 설치를 중단함: $WRAPPER" >&2
  exit 1
fi
chmod +x "$WRAPPER"

# 계정 원문은 이메일을 포함할 수 있으므로 설치 로그에 절대 출력하지 않는다.
if ! "$UV_BIN" run python -c 'from reporter.claude_cli_analyzer import check_cli_auth; check_cli_auth()' >/dev/null 2>&1; then
  echo "Claude CLI 구독 인증을 확인하지 못해 설치를 중단함" >&2
  exit 1
fi

LAUNCHD_PATH="$(dirname "$UV_BIN"):$(dirname "$CLAUDE_BIN"):/usr/bin:/bin"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>$WRAPPER</string></array>
<key>WorkingDirectory</key><string>$REPO_DIR</string>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>30</integer></dict>
<key>StandardOutPath</key><string>/tmp/vlm-backfill-finalizer.log</string>
<key>StandardErrorPath</key><string>/tmp/vlm-backfill-finalizer.log</string>
<key>EnvironmentVariables</key><dict>
<key>PATH</key><string>$LAUNCHD_PATH</string>
<key>HOME</key><string>$HOME</string>
<key>USER</key><string>$RUN_USER</string>
<key>LOGNAME</key><string>$RUN_USER</string>
</dict>
</dict></plist>
PLISTEOF

if ! plutil -lint "$PLIST" >/dev/null; then
  echo "plist 문법 오류로 설치를 중단함" >&2
  exit 1
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "installed $LABEL — 오늘 20:30 KST 1회 발화 후 스스로 해제됨(self-unload)"
echo "log: /tmp/vlm-backfill-finalizer.log"
echo "rollback (발화 전 취소): launchctl bootout gui/\$(id -u)/$LABEL && rm -f \"$PLIST\""
