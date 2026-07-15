import os
import plistlib
import subprocess
from pathlib import Path


def test_backfill_installer_is_hourly_subscription_only_and_shadow_safe():
    script = Path("install-launchd-vlm-backfill.sh").read_text()
    assert 'LABEL="com.petcam.vlm-historical-backfill"' in script
    assert "reporter.vlm_backfill_worker" in script
    assert "<key>RunAtLoad</key><true/>" in script
    assert "<key>StartInterval</key><integer>3600</integer>" in script
    assert "VLM_PROVIDER</key><string>claude_cli_batch" in script
    assert "ANTHROPIC_MODEL_EXACT</key><string>claude-sonnet-5" in script
    assert "REGISTER_HIGHLIGHTS</key><string>0" in script
    assert "<key>HOME</key><string>$HOME</string>" in script
    assert "<key>USER</key><string>$RUN_USER</string>" in script
    assert "<key>LOGNAME</key><string>$RUN_USER</string>" in script


def test_backfill_installer_has_fail_closed_preflights_and_no_secrets_in_plist():
    script = Path("install-launchd-vlm-backfill.sh").read_text()
    assert "check_cli_auth" in script
    assert ">/dev/null 2>&1" in script
    assert 'GATE_CHECKPOINT_PATH' in script
    assert 'if [ ! -f "$CHECKPOINT" ]' in script
    assert "plutil -lint" in script
    assert "SUPABASE_SERVICE_ROLE_KEY</key>" not in script
    assert "R2_SECRET_ACCESS_KEY</key>" not in script


def test_backfill_installer_renders_valid_plist_without_real_bootstrap(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    checkpoint = tmp_path / "gate.pth"
    checkpoint.write_bytes(b"checkpoint")
    (bin_dir / "claude").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "uv").write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *GATE_CHECKPOINT_PATH*) printf '%s\\n' \"$CHECKPOINT_STUB\" ;;\n"
        "esac\n"
    )
    for command in ("claude", "launchctl", "uv"):
        (bin_dir / command).chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CHECKPOINT_STUB": str(checkpoint),
    }
    subprocess.run(["bash", "install-launchd-vlm-backfill.sh"], check=True, env=env, capture_output=True, text=True)
    plist_path = tmp_path / "Library/LaunchAgents/com.petcam.vlm-historical-backfill.plist"
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["ProgramArguments"][-1] == "reporter.vlm_backfill_worker"
    assert payload["EnvironmentVariables"]["ANTHROPIC_MODEL_EXACT"] == "claude-sonnet-5"
    assert payload["EnvironmentVariables"]["REGISTER_HIGHLIGHTS"] == "0"
