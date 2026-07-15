from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import reporter.vlm_candidate_worker as vcw
from reporter.vlm_host_guard import HostOwnershipError, require_expected_host


def test_exact_hostname_match_passes():
    require_expected_host("mac-mini.verified", "mac-mini.verified")  # no raise


@pytest.mark.parametrize("expected", [None, "", "   "])
def test_blank_expected_fails_closed(expected):
    with pytest.raises(HostOwnershipError):
        require_expected_host("mac-mini.verified", expected)


def test_macbook_actual_vs_macmini_expected_fails():
    with pytest.raises(HostOwnershipError):
        require_expected_host("macbook.local", "mac-mini.verified")


def test_whitespace_normalized_then_exact_compare():
    require_expected_host("mac-mini.verified", "  mac-mini.verified  ")


def test_short_name_not_auto_equated_with_fqdn():
    with pytest.raises(HostOwnershipError):
        require_expected_host("mac-mini", "mac-mini.verified")


def test_error_label_drops_control_characters():
    with pytest.raises(HostOwnershipError) as caught:
        require_expected_host("mac\nmini\r0", "mac-mini.verified")
    assert "\n" not in str(caught.value) and "\r" not in str(caught.value)


def test_host_mismatch_stops_before_any_dependency(monkeypatch):
    calls = []
    monkeypatch.setattr(vcw, "create_client", lambda *a, **k: calls.append("create_client"))
    monkeypatch.setattr(vcw, "load_window_candidates", lambda *a, **k: calls.append("load_window") or [])

    def must_not_process(*_a, **_k):
        calls.append("process"); return {}

    rc = vcw.run(
        now=datetime(2026, 7, 16, 2, tzinfo=ZoneInfo("Asia/Seoul")), enabled=True,
        expected_host="mac-mini.verified", hostname_fn=lambda: "macbook.local",
        process_fn=must_not_process,
    )
    assert rc != 0
    assert calls == []
