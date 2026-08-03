#!/usr/bin/env bash
# GME production-shadow 60초 one-shot LaunchAgent installer. credential은 plist에 넣지 않고 repo .env에서 읽는다.
set -euo pipefail

LABEL="com.petcam.gme-worker"
INTERVAL_SEC="${GME_INTERVAL_SEC:-60}"
ENABLE="${GME_ENABLED:-0}"
EXPECTED_HOST="${GME_EXPECTED_HOST:-}"
BATCH_LIMIT="${GME_BATCH_LIMIT:-4}"
ACTUAL_HOST="$(hostname)"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

[ "$ENABLE" = "1" ] || { echo "GME_ENABLED=1 required" >&2; exit 1; }
[ -n "$EXPECTED_HOST" ] || { echo "GME_EXPECTED_HOST required" >&2; exit 1; }
[ "$ACTUAL_HOST" = "$EXPECTED_HOST" ] || { echo "hostname mismatch" >&2; exit 1; }
[ "$INTERVAL_SEC" = "60" ] || { echo "GME_INTERVAL_SEC must be 60" >&2; exit 1; }
[[ "$BATCH_LIMIT" =~ ^[0-9]+$ ]] && [ "$BATCH_LIMIT" -ge 1 ] && [ "$BATCH_LIMIT" -le 50 ] || {
  echo "GME_BATCH_LIMIT must be 1..50" >&2
  exit 1
}

UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || { echo "uv missing" >&2; exit 1; }
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
mkdir -p "$PLIST_DIR"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>reporter.gme_worker</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>/tmp/gme-worker.log</string>
  <key>StandardErrorPath</key><string>/tmp/gme-worker.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$UV_BIN"):/usr/bin:/bin</string>
    <key>GME_ENABLED</key><string>1</string>
    <key>GME_EXPECTED_HOST</key><string>$EXPECTED_HOST</string>
    <key>GME_BATCH_LIMIT</key><string>$BATCH_LIMIT</string>
  </dict>
</dict></plist>
PLIST

plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL interval=60s"
