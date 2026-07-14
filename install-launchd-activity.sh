#!/usr/bin/env bash
# 활동필터 worker LaunchAgent 설치 — 기존 nightly-reporter(상황판, com.petcam.nightly-reporter)와
# 완전 별도 job. 활동 worker 는 Claude/VLM 0회 → uv 만 필수(claude/ffmpeg 불필요).
# Gate(RF-DETR) 는 python editable 의존성으로 같은 프로세스에서 재사용(모델 재로드 금지).
#
# 왜 launchd 인가: cron 은 GUI 세션 밖이라 keychain/구독 인증 문제(메모리 cron-launchd-keychain).
# 활동 worker 는 claude 를 안 쓰지만, 상황판 worker 와 일관되게 LaunchAgent 로 통일.
set -euo pipefail

LABEL="com.petcam.activity-worker"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  echo "❌ uv 를 PATH 에서 못 찾음 — 'command -v uv' 확인 후 재시도" >&2
  exit 1
fi
BIN_DIRS="$(dirname "$UV_BIN")"
LAUNCHD_PATH="$BIN_DIRS:/usr/bin:/bin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"

# 1시간마다: allowlist 카메라의 미처리 motion_clips 를 Gate 로 분석. 설정(enabled) 없으면 즉시 0건 종료.
# flock 으로 중복 실행 방지(긴 backfill 이 다음 tick 과 겹쳐도 안전).
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>reporter.activity_worker</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>3600</integer>
    <key>StandardOutPath</key><string>/tmp/activity-worker.log</string>
    <key>StandardErrorPath</key><string>/tmp/activity-worker.log</string>
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
echo "   즉시 1회(RunAtLoad) + 1시간마다. 설정 없으면 0건 종료(앱 제외 없음)."
echo "   로그:  tail -f /tmp/activity-worker.log"
echo "   상태:  launchctl print gui/\$(id -u)/$LABEL | grep -Ei 'state|last exit'"
echo "   중지:  launchctl bootout gui/\$(id -u)/$LABEL && rm \"$PLIST\""
