#!/usr/bin/env bash
# 2026-07-15 20:30 KST 1회성 finalizer 실행체. LaunchAgent가 이 스크립트를 부른다.
# /opt/homebrew/bin/claude 의 구독(GUI keychain) 인증으로 handoff prompt를 headless 실행하고,
# 성공/실패/중단 어느 경로든 항상 자기 자신을 launchd에서 해제해 딱 1회만 발화하게 한다.
set -eo pipefail

LABEL="com.petcam.vlm-backfill-finalizer"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROMPT_FILE="$REPO_DIR/scripts/finalizer_handoff_prompt.md"
CLAUDE_BIN="/opt/homebrew/bin/claude"

self_unload() {
  # 성공/실패/중단 어느 경로로 종료돼도 EXIT trap이 반드시 1회 실행돼 재발화를 막는다.
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  rm -f "$PLIST"
  echo "[finalizer] self-unloaded $LABEL (one-shot complete)"
}
trap self_unload EXIT

if [ ! -x "$CLAUDE_BIN" ]; then
  echo "[finalizer] $CLAUDE_BIN 을 찾지 못함 — 판정 없이 종료" >&2
  exit 1
fi
if [ ! -f "$PROMPT_FILE" ]; then
  echo "[finalizer] prompt 파일이 없음: $PROMPT_FILE" >&2
  exit 1
fi

cd "$REPO_DIR"
echo "[finalizer] start $(date -u +%FT%TZ)"
"$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
  --permission-mode bypassPermissions \
  --allowedTools "Bash Read Write Edit Grep Glob" \
  --output-format text \
  --no-session-persistence
echo "[finalizer] end $(date -u +%FT%TZ)"
