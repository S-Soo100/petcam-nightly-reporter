#!/usr/bin/env bash
# 정규 VLM 후보 worker(§6.2). production host 는 검증된 Mac mini 한 대뿐이며, 모든 guard 를
# 통과해야 bootstrap 한다. 현재 hostname 을 expected 로 자동 복사하지 않는다(자기 승인 금지).
set -euo pipefail
LABEL="com.petcam.vlm-candidate-worker"
ENABLE="${VLM_ROUTER_ENABLED:-0}"
PROVIDER="${VLM_PROVIDER:-claude_cli_batch}"
MODEL="${ANTHROPIC_MODEL_EXACT:-claude-sonnet-5}"
EXPECTED_HOST="${VLM_EXPECTED_HOST:-}"
RUN_USER="$(id -un)"
ACTUAL_HOST="$(hostname)"

# --- fail-closed production guards ---
[[ "$ENABLE" == "1" ]] || { echo "production install requires VLM_ROUTER_ENABLED=1" >&2; exit 1; }
[[ "$PROVIDER" == "claude_cli_batch" ]] || { echo "production install requires VLM_PROVIDER=claude_cli_batch" >&2; exit 1; }
[[ "$MODEL" == "claude-sonnet-5" ]] || { echo "ANTHROPIC_MODEL_EXACT must be claude-sonnet-5" >&2; exit 1; }
[[ -n "$EXPECTED_HOST" ]] || { echo "VLM_EXPECTED_HOST required (verified Mac mini hostname)" >&2; exit 1; }
[[ "$ACTUAL_HOST" == "$EXPECTED_HOST" ]] || { echo "hostname mismatch — refusing install on non-expected host" >&2; exit 1; }

UV_BIN="$(command -v uv)"; CLAUDE_BIN="$(command -v claude || true)"
[[ -n "$CLAUDE_BIN" ]] || { echo "claude CLI not found in PATH" >&2; exit 1; }
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"; PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LAUNCHD_PATH="$(dirname "$UV_BIN"):$(dirname "$CLAUDE_BIN"):/usr/bin:/bin"
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
<key>EnvironmentVariables</key><dict><key>PATH</key><string>$LAUNCHD_PATH</string><key>HOME</key><string>$HOME</string><key>USER</key><string>$RUN_USER</string><key>LOGNAME</key><string>$RUN_USER</string><key>VLM_ROUTER_ENABLED</key><string>$ENABLE</string><key>VLM_PROVIDER</key><string>$PROVIDER</string><key>VLM_EXPECTED_HOST</key><string>$EXPECTED_HOST</string><key>REGISTER_HIGHLIGHTS</key><string>0</string><key>ANTHROPIC_MODEL_EXACT</key><string>$MODEL</string></dict>
</dict></plist>
EOF
plutil -lint "$PLIST" >/dev/null || { echo "plist lint failed — not bootstrapping" >&2; exit 1; }
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL enabled=$ENABLE provider=$PROVIDER model=$MODEL host=$EXPECTED_HOST schedule=22/00/02/04KST"
