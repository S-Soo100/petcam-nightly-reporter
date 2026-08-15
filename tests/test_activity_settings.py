"""activity_settings.load_enabled_cameras — 카메라별 스위치 allowlist.

설정 row 없음/enabled=false = 필터 비활성(빈 allowlist) → worker 0건 (지시문 §94·§190).
"""

from tests._fakes import FakeSB

from reporter.activity_settings import CameraFilterSetting, load_enabled_cameras


def test_only_enabled_cameras_returned():
    sb = FakeSB({"camera_activity_filter_settings": [
        {"camera_id": "A", "enabled": True, "exclude_absent_enabled": True,
         "exclude_static_enabled": False, "active_policy_version": "p1"},
        {"camera_id": "B", "enabled": False, "exclude_absent_enabled": True,
         "exclude_static_enabled": True, "active_policy_version": "p1"},
    ]})
    out = load_enabled_cameras(sb)
    assert [c.camera_id for c in out] == ["A"]
    assert isinstance(out[0], CameraFilterSetting)
    assert out[0].exclude_absent_enabled is True
    assert out[0].exclude_static_enabled is False
    assert out[0].active_policy_version == "p1"


def test_no_settings_row_returns_empty():
    sb = FakeSB({})
    assert load_enabled_cameras(sb) == []


def test_all_disabled_returns_empty():
    sb = FakeSB({"camera_activity_filter_settings": [
        {"camera_id": "A", "enabled": False, "exclude_absent_enabled": False,
         "exclude_static_enabled": False, "active_policy_version": None},
    ]})
    assert load_enabled_cameras(sb) == []
