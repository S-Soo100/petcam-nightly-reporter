#!/usr/bin/env bash
# GME production-shadow 60초 one-shot LaunchAgent installer. credential은 plist에 넣지 않고 repo .env에서 읽는다.
set -euo pipefail

LABEL="com.petcam.gme-worker"
INTERVAL_SEC="${GME_INTERVAL_SEC:-60}"
ENABLE="${GME_ENABLED:-0}"
EXPECTED_HOST="${GME_EXPECTED_HOST:-}"
BATCH_LIMIT="${GME_BATCH_LIMIT:-4}"
DETECTOR_BACKEND="${GME_DETECTOR_BACKEND:-}"
CHECKPOINT_PATH="${GME_CHECKPOINT_PATH:-}"
CHECKPOINT_SHA256="${GME_CHECKPOINT_SHA256:-}"
RAW_CONFIDENCE="${GME_RAW_CONFIDENCE:-}"
SCORE_THRESHOLD="${GME_SCORE_THRESHOLD:-}"
IMAGE_SIZE="${GME_IMAGE_SIZE:-}"
NMS_IOU="${GME_NMS_IOU:-}"
MAX_DETECTIONS="${GME_MAX_DETECTIONS:-}"
DEVICE="${GME_DEVICE:-}"
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
[ "$DETECTOR_BACKEND" = "yolo26n" ] || { echo "GME_DETECTOR_BACKEND must be yolo26n" >&2; exit 1; }
[[ "$CHECKPOINT_PATH" = /* ]] || { echo "GME_CHECKPOINT_PATH must be absolute" >&2; exit 1; }
[[ "$CHECKPOINT_SHA256" =~ ^[0-9a-f]{64}$ ]] || { echo "GME_CHECKPOINT_SHA256 invalid" >&2; exit 1; }
[ "$RAW_CONFIDENCE" = "0.001" ] || { echo "GME_RAW_CONFIDENCE must be 0.001" >&2; exit 1; }
[ "$SCORE_THRESHOLD" = "0.20" ] || { echo "GME_SCORE_THRESHOLD must be 0.20" >&2; exit 1; }
[ "$IMAGE_SIZE" = "960" ] || { echo "GME_IMAGE_SIZE must be 960" >&2; exit 1; }
[ "$NMS_IOU" = "0.70" ] || { echo "GME_NMS_IOU must be 0.70" >&2; exit 1; }
[ "$MAX_DETECTIONS" = "50" ] || { echo "GME_MAX_DETECTIONS must be 50" >&2; exit 1; }
[ "$DEVICE" = "mps" ] || { echo "GME_DEVICE must be mps" >&2; exit 1; }

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
    <key>GME_DETECTOR_BACKEND</key><string>$DETECTOR_BACKEND</string>
    <key>GME_CHECKPOINT_PATH</key><string>$CHECKPOINT_PATH</string>
    <key>GME_CHECKPOINT_SHA256</key><string>$CHECKPOINT_SHA256</string>
    <key>GME_RAW_CONFIDENCE</key><string>$RAW_CONFIDENCE</string>
    <key>GME_SCORE_THRESHOLD</key><string>$SCORE_THRESHOLD</string>
    <key>GME_IMAGE_SIZE</key><string>$IMAGE_SIZE</string>
    <key>GME_NMS_IOU</key><string>$NMS_IOU</string>
    <key>GME_MAX_DETECTIONS</key><string>$MAX_DETECTIONS</string>
    <key>GME_DEVICE</key><string>$DEVICE</string>
  </dict>
</dict></plist>
PLIST

plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL interval=60s"
