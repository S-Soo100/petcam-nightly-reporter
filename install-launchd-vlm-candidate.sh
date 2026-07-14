#!/usr/bin/env bash
set -euo pipefail
LABEL="com.petcam.vlm-candidate-worker"
ENABLE="${VLM_ROUTER_ENABLED:-0}"
[[ "$ENABLE" == "0" || "$ENABLE" == "1" ]] || { echo "VLM_ROUTER_ENABLED must be 0 or 1" >&2; exit 1; }
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"; UV_BIN="$(command -v uv)"; PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>reporter.vlm_candidate_worker</string></array>
<key>WorkingDirectory</key><string>$REPO_DIR</string>
<key>StartCalendarInterval</key><array>
<dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer></dict>
<dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>0</integer></dict>
<dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer></dict>
<dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
</array>
<key>StandardOutPath</key><string>/tmp/vlm-candidate-worker.log</string><key>StandardErrorPath</key><string>/tmp/vlm-candidate-worker.log</string>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>$(dirname "$UV_BIN"):/usr/bin:/bin</string><key>VLM_ROUTER_ENABLED</key><string>$ENABLE</string><key>REGISTER_HIGHLIGHTS</key><string>0</string><key>ANTHROPIC_MODEL_EXACT</key><string>claude-sonnet-5</string></dict>
</dict></plist>
EOF
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL enabled=$ENABLE"
