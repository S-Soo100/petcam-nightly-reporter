"""camera_activity_filter_settings 조회 — 활동필터 카메라 allowlist.

설정 row 가 없거나 enabled=false 면 필터 비활성(빈 allowlist). worker 는 빈 allowlist 면
0건 처리한다 → 새 카메라·미설정 카메라는 자동 적용되지 않는다(지시문 §94~95·§190).
"""

from __future__ import annotations

from dataclasses import dataclass

_FIELDS = (
    "camera_id",
    "enabled",
    "exclude_absent_enabled",
    "exclude_static_enabled",
    "active_policy_version",
)


@dataclass(frozen=True, slots=True)
class CameraFilterSetting:
    camera_id: str
    enabled: bool
    exclude_absent_enabled: bool
    exclude_static_enabled: bool
    active_policy_version: str | None


def load_enabled_cameras(sb) -> list[CameraFilterSetting]:
    """enabled=true 인 카메라 설정만 반환. service_role client 로 조회."""
    rows = (
        sb.table("camera_activity_filter_settings")
        .select(", ".join(_FIELDS))
        .eq("enabled", True)
        .execute()
        .data
    )
    return [CameraFilterSetting(**{k: r[k] for k in _FIELDS}) for r in rows]
