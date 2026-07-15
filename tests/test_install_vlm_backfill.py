import os
import plistlib
import subprocess
from pathlib import Path


def test_backfill_installer_is_daytime_calendar_subscription_only_and_shadow_safe():
    # 새 계약(§7.2): RunAtLoad/StartInterval 제거, 07~19시 정각 calendar 만 — 정규 야간 lock 경합 차단.
    script = Path("install-launchd-vlm-backfill.sh").read_text()
    assert 'LABEL="com.petcam.vlm-historical-backfill"' in script
    assert "reporter.vlm_backfill_worker" in script
    assert "RunAtLoad" not in script
    assert "<key>StartInterval</key>" not in script
    assert "<key>StartCalendarInterval</key>" in script
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
    assert "RunAtLoad" not in payload
    assert "StartInterval" not in payload
    hours = sorted(entry["Hour"] for entry in payload["StartCalendarInterval"])
    assert hours == list(range(7, 20))  # 07~19시 정각만
    assert all(entry["Minute"] == 0 for entry in payload["StartCalendarInterval"])
    assert not (set(hours) & {20, 21, 22, 23, 0, 2, 4})  # 정규 야간 schedule 과 무겹침
    assert payload["EnvironmentVariables"]["ANTHROPIC_MODEL_EXACT"] == "claude-sonnet-5"
    assert payload["EnvironmentVariables"]["REGISTER_HIGHLIGHTS"] == "0"
