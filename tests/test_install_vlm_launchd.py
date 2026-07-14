from pathlib import Path
def test_installer_contract():
    t=Path("install-launchd-vlm-candidate.sh").read_text()
    assert t.count("<key>Hour</key>")==4
    assert "REGISTER_HIGHLIGHTS</key><string>0" in t
    assert "RunAtLoad" not in t and "plutil -lint" in t
