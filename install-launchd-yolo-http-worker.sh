#!/usr/bin/env bash
# localhost 전용 v2.6 HTTP worker. token은 repo .env에서 worker가 직접 읽고 plist에는 쓰지 않는다.
set -euo pipefail

LABEL="com.petcam.yolo-http-worker"
EXPECTED_HOST="${YOLO_HTTP_EXPECTED_HOST:-}"
ACTUAL_HOST="$(hostname)"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$REPO_DIR/.env"

[ -n "$EXPECTED_HOST" ] || { echo "YOLO_HTTP_EXPECTED_HOST required" >&2; exit 1; }
[ "$ACTUAL_HOST" = "$EXPECTED_HOST" ] || { echo "hostname mismatch" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo ".env required" >&2; exit 1; }
ENV_MODE="$(stat -f '%Lp' "$ENV_FILE")"
[[ "$ENV_MODE" =~ ^[0-7]{3,4}$ ]] || { echo ".env mode invalid" >&2; exit 1; }
(( (8#$ENV_MODE & 077) == 0 )) || { echo ".env must not be group/world readable" >&2; exit 1; }
TOKEN_LINE="$(grep '^YOLO_HTTP_WORKER_TOKEN=' "$ENV_FILE" | tail -n 1 || true)"
[ -n "${TOKEN_LINE#YOLO_HTTP_WORKER_TOKEN=}" ] || { echo "YOLO_HTTP_WORKER_TOKEN required" >&2; exit 1; }

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
    <string>$UV_BIN</string><string>run</string><string>uvicorn</string>
    <string>reporter.yolo_http_worker:app</string><string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/yolo-http-worker.log</string>
  <key>StandardErrorPath</key><string>/tmp/yolo-http-worker.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$UV_BIN"):/usr/bin:/bin</string>
  </dict>
</dict></plist>
PLIST

plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL localhost:8765"
