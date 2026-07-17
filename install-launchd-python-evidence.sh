#!/usr/bin/env bash
# 전 영상 Python evidence worker LaunchAgent 설치 — activity/상황판 worker 와 완전 별도 job.
# Claude/VLM 0회 → uv 만 필수. Gate(RF-DETR) 는 python editable 의존성으로 같은 프로세스 재사용.
#
# 왜 launchd 인가: cron 은 GUI 세션 밖이라 keychain/구독 인증 문제(메모리 cron-launchd-keychain).
#
# ⚠️ 이 스크립트는 S2B 배포 아티팩트다. plan Stop Point 상 **production 실제 설치는 금지**이며,
#    테스트는 temp HOME + stub launchctl 로만 검증한다. 아래 fail-closed 가드 3종을 통과해야만 설치된다.
set -euo pipefail

LABEL="com.petcam.python-evidence-worker"
# 30분 간격 폴링(shadow). S2B 에서 조정. cron 아님(launchd) — GUI 세션 인증 전제.
INTERVAL_SEC="${PYTHON_EVIDENCE_INTERVAL_SEC:-1800}"
ENABLE="${PYTHON_EVIDENCE_ENABLED:-0}"
EXPECTED_HOST="${PYTHON_EVIDENCE_EXPECTED_HOST:-}"
ACTUAL_HOST="$(hostname)"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- fail-closed production 가드 (§11) ---
# 1) 반드시 enabled=1 이어야 설치(운영 명시 opt-in). 2) EXPECTED_HOST 명시 필수(자동 승인 금지).
# 3) 실제 hostname 과 정확히 일치할 때만 설치(non-expected host 거부).
[ "$ENABLE" = "1" ] || { echo "❌ PYTHON_EVIDENCE_ENABLED=1 필요 — 설치 중단" >&2; exit 1; }
[ -n "$EXPECTED_HOST" ] || { echo "❌ PYTHON_EVIDENCE_EXPECTED_HOST required (검증된 Mac mini hostname) — 설치 중단" >&2; exit 1; }
[ "$ACTUAL_HOST" = "$EXPECTED_HOST" ] || { echo "❌ hostname mismatch (actual=$ACTUAL_HOST expected=$EXPECTED_HOST) — 설치 거부" >&2; exit 1; }

UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || { echo "❌ uv 를 PATH 에서 못 찾음 — 'command -v uv' 확인 후 재시도" >&2; exit 1; }
BIN_DIRS="$(dirname "$UV_BIN")"
LAUNCHD_PATH="$BIN_DIRS:/usr/bin:/bin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"

# 30분마다: durable queue 의 due job 을 claim → Level 0/1 evidence 저장. backlog 없으면 detector/R2 0.
# 공통 Gate flock 으로 activity worker 와 detector 상호배제(loser clean no-op).
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string><string>run</string><string>python</string><string>-m</string><string>reporter.python_evidence_worker</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO_DIR</string>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>$INTERVAL_SEC</integer>
    <key>StandardOutPath</key><string>/tmp/python-evidence-worker.log</string>
    <key>StandardErrorPath</key><string>/tmp/python-evidence-worker.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$LAUNCHD_PATH</string>
        <key>PYTHON_EVIDENCE_ENABLED</key><string>$ENABLE</string>
        <key>PYTHON_EVIDENCE_EXPECTED_HOST</key><string>$EXPECTED_HOST</string>
    </dict>
</dict>
</plist>
PLISTEOF

# plist 문법 검증 — 실패하면 설치하지 않고 종료(깨진 plist 로 bootstrap 방지)
if ! plutil -lint "$PLIST" >/dev/null; then
  echo "❌ plist lint 실패 — 설치 중단: $PLIST" >&2
  exit 1
fi

# 멱등: 이미 등록돼 있으면 먼저 해제하고 재등록
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ installed + bootstrapped: $PLIST"
echo "   PATH=$LAUNCHD_PATH"
echo "   PYTHON_EVIDENCE_EXPECTED_HOST=$EXPECTED_HOST  INTERVAL=${INTERVAL_SEC}s"
echo "   로그:  tail -f /tmp/python-evidence-worker.log"
echo "   중지:  launchctl bootout gui/\$(id -u)/$LABEL && rm \"$PLIST\""
