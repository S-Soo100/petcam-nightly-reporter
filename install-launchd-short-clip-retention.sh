#!/usr/bin/env bash
# 짧은 영상 장치 오류 격리·보존 worker LaunchAgent 설치 — 다른 worker 와 완전 별도 job.
# metadata-only 감지(+7일 보존 후 exact R2 삭제). download/OpenCV/Gate/detector/VLM 0 → uv 만 필수.
#
# 왜 launchd 인가: cron 은 GUI 세션 밖이라 keychain/인증 문제(메모리 cron-launchd-keychain).
#
# ⚠️ 이 스크립트는 배포 아티팩트다. 이번 handoff Stop Point 상 **production 실제 설치는 금지**이며,
#    테스트는 temp HOME + stub launchctl/plutil 로 render 만 검증한다. 아래 fail-closed 가드를
#    통과할 때만(EXPECTED_HOST 명시 + 실제 hostname 일치) plist 를 쓰고 bootstrap 한다.
set -euo pipefail

LABEL="com.petcam.short-clip-retention"
MODULE="reporter.short_clip_retention_worker"
# 1시간 간격 폴링. cron 아님(launchd) — GUI 세션 인증 전제.
INTERVAL_SEC="${SHORT_CLIP_RETENTION_INTERVAL_SEC:-3600}"
# 기본값: 감지 enabled=1(shadow), write=0/delete=0(운영 효과 0). 배포 단계에서 개별 승인 후 상향.
ENABLE="${SHORT_CLIP_RETENTION_ENABLED:-1}"
WRITE="${SHORT_CLIP_RETENTION_WRITE_ENABLED:-0}"
DELETE="${SHORT_CLIP_RETENTION_DELETE_ENABLED:-0}"
EXPECTED_HOST="${SHORT_CLIP_RETENTION_EXPECTED_HOST:-}"
ACTUAL_HOST="$(hostname)"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- fail-closed production 가드 (설계 §9) ---
# 1) EXPECTED_HOST 명시 필수(현재 hostname 을 자동 승인·복사하지 않는다).
# 2) 실제 hostname 과 정확히 일치할 때만 설치(non-expected host 는 rendering/bootstrap 전에 거부).
[ -n "$EXPECTED_HOST" ] || { echo "❌ SHORT_CLIP_RETENTION_EXPECTED_HOST required (검증된 Mac mini hostname) — 설치 중단" >&2; exit 1; }
[ "$ACTUAL_HOST" = "$EXPECTED_HOST" ] || { echo "❌ hostname mismatch (actual=$ACTUAL_HOST expected=$EXPECTED_HOST) — 설치 거부" >&2; exit 1; }

UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || { echo "❌ uv 를 PATH 에서 못 찾음 — 'command -v uv' 확인 후 재시도" >&2; exit 1; }
BIN_DIRS="$(dirname "$UV_BIN")"
LAUNCHD_PATH="$BIN_DIRS:/usr/bin:/bin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"

# 1시간마다: <15s 후보를 metadata 로 감지·기록(write=1일 때). delete=1일 때만 7일 만료분 exact 삭제.
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>$MODULE</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>$INTERVAL_SEC</integer>
    <key>StandardOutPath</key><string>/tmp/short-clip-retention-worker.log</string>
    <key>StandardErrorPath</key><string>/tmp/short-clip-retention-worker.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$LAUNCHD_PATH</string>
        <key>SHORT_CLIP_RETENTION_ENABLED</key><string>$ENABLE</string>
        <key>SHORT_CLIP_RETENTION_WRITE_ENABLED</key><string>$WRITE</string>
        <key>SHORT_CLIP_RETENTION_DELETE_ENABLED</key><string>$DELETE</string>
        <key>SHORT_CLIP_RETENTION_EXPECTED_HOST</key><string>$EXPECTED_HOST</string>
    </dict>
</dict>
</plist>
PLISTEOF

# plist 문법 검증 — 실패하면 설치하지 않고 종료(깨진 plist 로 bootstrap 방지). bootstrap 이전에 수행.
if ! plutil -lint "$PLIST" >/dev/null; then
  echo "❌ plist lint 실패 — 설치 중단: $PLIST" >&2
  exit 1
fi

# 멱등: 이미 등록돼 있으면 먼저 해제하고 재등록.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ installed + bootstrapped: $PLIST"
echo "   PATH=$LAUNCHD_PATH"
echo "   SHORT_CLIP_RETENTION_ENABLED=$ENABLE  SHORT_CLIP_RETENTION_WRITE_ENABLED=$WRITE  SHORT_CLIP_RETENTION_DELETE_ENABLED=$DELETE"
echo "   SHORT_CLIP_RETENTION_EXPECTED_HOST=$EXPECTED_HOST  INTERVAL=${INTERVAL_SEC}s"
echo "   로그:  tail -f /tmp/short-clip-retention-worker.log"
echo "   중지:  launchctl bootout gui/\$(id -u)/$LABEL && rm \"$PLIST\""
