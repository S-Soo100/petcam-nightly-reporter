#!/usr/bin/env bash
# camera_clips labeling triage preview worker. Committed launcher never writes suggestions.
set -euo pipefail

LABEL="com.petcam.labeling-triage-worker"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  echo "uv not found" >&2
  exit 1
fi

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LAUNCHD_PATH="$(dirname "$UV_BIN"):/usr/bin:/bin"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>reporter.labeling_triage_worker</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>/tmp/labeling-triage-worker.log</string>
  <key>StandardErrorPath</key><string>/tmp/labeling-triage-worker.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$LAUNCHD_PATH</string>
    <key>LABELING_TRIAGE_ENABLED</key><string>1</string>
    <key>LABELING_TRIAGE_WRITE_ENABLED</key><string>0</string>
    <key>LABELING_TRIAGE_POLICY_VERSION</key><string>labeling-triage-v1</string>
    <key>LABELING_TRIAGE_ACTIVITY_POLICY_VERSION</key><string>activity-v1</string>
  </dict>
</dict></plist>
PLISTEOF

plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "installed preview-only worker: $PLIST"
echo "LABELING_TRIAGE_WRITE_ENABLED=0 (triage DB write 없음)"
