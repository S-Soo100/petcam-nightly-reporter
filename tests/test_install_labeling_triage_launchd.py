from pathlib import Path


def test_launcher_is_preview_only_and_lints_before_bootstrap():
    script = Path("install-launchd-labeling-triage.sh").read_text()
    assert 'LABEL="com.petcam.labeling-triage-worker"' in script
    assert "reporter.labeling_triage_worker" in script
    assert "LABELING_TRIAGE_ENABLED</key><string>1" in script
    assert "LABELING_TRIAGE_WRITE_ENABLED</key><string>0" in script
    assert "LABELING_TRIAGE_POLICY_VERSION</key><string>labeling-triage-v1" in script
    assert "<key>StartInterval</key><integer>3600</integer>" in script
    assert "<key>PATH</key>" in script
    assert script.index('plutil -lint "$PLIST"') < script.index('launchctl bootstrap')
