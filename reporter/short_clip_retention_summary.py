"""짧은 영상 retention 일일 Slack 카드 formatter (설계 §8).

count 와 안전 라벨(장비/규칙)만 렌더한다. raw R2 key/URL/UUID/lease token/fingerprint/DB message/
endpoint/exception 은 절대 담지 않는다 — stats dict 에 그런 키가 섞여 있어도 무시하고 allowlist
필드만 int 로 뽑는다.
"""

from __future__ import annotations

from datetime import datetime

_DEFAULT_DEVICE = "Mac mini"
_DEFAULT_RULE = "short-device-error-v1"


def _count(stats: dict, key: str) -> int:
    value = stats.get(key, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def format_short_clip_retention_summary(stats: dict, now_kst: datetime) -> str:
    """§8 형식의 1일 카드. stats 는 count(+ device_label/rule_version 라벨)만 쓴다."""
    candidate = _count(stats, "candidate")
    quarantined = _count(stats, "quarantined")
    review_pending = _count(stats, "review_pending")
    restored = _count(stats, "restored")
    pending_delete = _count(stats, "pending_delete")
    deleted = _count(stats, "deleted")
    blocked = _count(stats, "blocked")
    device = str(stats.get("device_label") or _DEFAULT_DEVICE)
    rule = str(stats.get("rule_version") or _DEFAULT_RULE)
    return (
        "🗑️ 짧은 영상 장치 오류\n"
        f"· 후보 {candidate} · 자동 제외 {quarantined} · 검수 대기 {review_pending}\n"
        f"· Owner 복구 {restored} · 7일 후 삭제 예정 {pending_delete}\n"
        f"· 오늘 R2 삭제 {deleted} · 삭제 차단 {blocked}\n"
        f"· 실행 장비: {device} · 규칙 {rule}"
    )
