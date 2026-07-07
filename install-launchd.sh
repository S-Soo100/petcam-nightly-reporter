#!/usr/bin/env bash
# LaunchAgent 설치 — 이 머신 기준($HOME·bin 경로)으로 plist 생성 후 bootstrap.
#
# 왜 cron 아니라 launchd 인가:
#   cron 잡은 GUI 로그인 세션 밖에서 돌아 claude 구독 인증(login keychain)에
#   접근 못 함 → `Not logged in`. LaunchAgent 는 GUI(Aqua) 세션에서 실행돼
#   keychain 접근 OK. (전제: 자동로그인 + GUI 세션 상시. 메모리 cron-launchd-keychain)
set -euo pipefail

LABEL="com.petcam.nightly-reporter"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# uv·claude·ffmpeg 3개 필수 (worker=uv run / classify=claude -p / frames=ffmpeg·ffprobe)
UV_BIN="$(command -v uv || true)"
CLAUDE_BIN="$(command -v claude || true)"
FFMPEG_BIN="$(command -v ffmpeg || true)"
for pair in "uv:$UV_BIN" "claude:$CLAUDE_BIN" "ffmpeg:$FFMPEG_BIN"; do
    if [ -z "${pair#*:}" ]; then
        echo "❌ ${pair%%:*} 를 PATH 에서 못 찾음 — 'command -v ${pair%%:*}' 확인 후 재시도" >&2
        exit 1
    fi
done

# launchd 는 .zshrc PATH 를 안 물려받음 → 3개 bin 디렉토리를 명시(중복 제거).
# 맥미니 실측 함정: uv=~/.local/bin, claude=/opt/homebrew/bin 처럼 서로 다른 디렉토리일 수 있음.
BIN_DIRS="$(printf '%s\n%s\n%s\n' \
    "$(dirname "$UV_BIN")" "$(dirname "$CLAUDE_BIN")" "$(dirname "$FFMPEG_BIN")" \
    | awk '!seen[$0]++' | paste -sd: -)"
LAUNCHD_PATH="$BIN_DIRS:/usr/bin:/bin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"

# 30분마다 상시(StartInterval) → 직전 30분(WINDOW_HOURS=0.5) 상황판. 활동 0 인 창은 worker
# 가 스킵(빈 카드 방지)이라 낮엔 조용. claude 는 SAMPLE_TOP_N=1(밤 top-1)로 한도 통제.
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>reporter.worker</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>1800</integer>
    <key>StandardOutPath</key><string>/tmp/nightly-reporter.log</string>
    <key>StandardErrorPath</key><string>/tmp/nightly-reporter.log</string>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>$LAUNCHD_PATH</string></dict>
</dict>
</plist>
PLISTEOF

# 멱등: 이미 등록돼 있으면 먼저 해제하고 재등록
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ installed + bootstrapped: $PLIST"
echo "   PATH=$LAUNCHD_PATH"
echo "   즉시 1회(RunAtLoad) + 30분마다(활동시 상황판 · 무활동시 스킵)"
echo "   로그:  tail -f /tmp/nightly-reporter.log"
echo "   해제:  launchctl bootout gui/\$(id -u)/$LABEL && rm \"$PLIST\""
