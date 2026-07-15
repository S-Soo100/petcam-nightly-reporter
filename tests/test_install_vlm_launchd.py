from pathlib import Path
def test_installer_contract():
    t=Path("install-launchd-vlm-candidate.sh").read_text()
    assert t.count("<key>Hour</key>")==4
    assert "VLM_PROVIDER</key><string>$PROVIDER" in t
    assert 'PROVIDER="${VLM_PROVIDER:-claude_cli_batch}"' in t
    assert "<key>HOME</key><string>$HOME</string>" in t
    assert "<key>USER</key><string>$RUN_USER</string>" in t
    assert "<key>LOGNAME</key><string>$RUN_USER</string>" in t
    assert "REGISTER_HIGHLIGHTS</key><string>0" in t
    assert "RunAtLoad" not in t and "plutil -lint" in t
