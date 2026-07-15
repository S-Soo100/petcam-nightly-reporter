import os
import plistlib
import subprocess
from pathlib import Path


def test_finalizer_installer_is_one_shot_calendar_and_keychain_safe():
    script = Path("install-launchd-vlm-backfill-finalizer.sh").read_text()
    assert 'LABEL="com.petcam.vlm-backfill-finalizer"' in script
    assert "run-vlm-backfill-finalizer.sh" in script
    assert "<key>Hour</key><integer>20</integer>" in script
    assert "<key>Minute</key><integer>30</integer>" in script
    assert "RunAtLoad" not in script
    assert "StartInterval" not in script
    assert "<key>HOME</key><string>$HOME</string>" in script
    assert "<key>USER</key><string>$RUN_USER</string>" in script
    assert "<key>LOGNAME</key><string>$RUN_USER</string>" in script
    assert "SUPABASE_SERVICE_ROLE_KEY</key>" not in script
    assert "SUPABASE_URL</key>" not in script
    assert "R2_SECRET_ACCESS_KEY</key>" not in script
    assert "R2_ACCESS_KEY_ID</key>" not in script
    assert "ANTHROPIC_API_KEY" not in script


def test_finalizer_installer_has_fail_closed_preflights_and_no_secrets_logged():
    script = Path("install-launchd-vlm-backfill-finalizer.sh").read_text()
    assert "check_cli_auth" in script
    assert ">/dev/null 2>&1" in script
    assert "plutil -lint" in script
    assert 'if [ ! -f "$WRAPPER" ]' in script


def test_finalizer_installer_renders_valid_plist_without_real_bootstrap(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "launchctl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "uv").write_text("#!/bin/sh\nexit 0\n")
    for command in ("claude", "launchctl", "uv"):
        (bin_dir / command).chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    subprocess.run(
        ["bash", "install-launchd-vlm-backfill-finalizer.sh"],
        check=True, env=env, capture_output=True, text=True, cwd=os.getcwd(),
    )
    plist_path = tmp_path / "Library/LaunchAgents/com.petcam.vlm-backfill-finalizer.plist"
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["ProgramArguments"] == [str(Path.cwd() / "run-vlm-backfill-finalizer.sh")]
    assert payload["StartCalendarInterval"] == {"Hour": 20, "Minute": 30}
    assert "RunAtLoad" not in payload
    assert "StartInterval" not in payload
    env_vars = payload["EnvironmentVariables"]
    assert env_vars["HOME"] == str(tmp_path)
    assert set(env_vars) == {"PATH", "HOME", "USER", "LOGNAME"}


def test_finalizer_wrapper_is_self_unloading_bypass_permission_and_uses_subscription_claude():
    wrapper = Path("run-vlm-backfill-finalizer.sh").read_text()
    assert "/opt/homebrew/bin/claude" in wrapper
    assert "--permission-mode" in wrapper
    assert "bypassPermissions" in wrapper
    assert "--allowedTools" in wrapper
    assert "finalizer_handoff_prompt.md" in wrapper
    assert "launchctl bootout" in wrapper
    assert "com.petcam.vlm-backfill-finalizer" in wrapper
    assert "trap self_unload EXIT" in wrapper
    assert "ANTHROPIC_API_KEY" not in wrapper
    assert "SUPABASE" not in wrapper
    assert "R2_SECRET" not in wrapper


def test_finalizer_wrapper_shell_syntax_is_valid():
    result = subprocess.run(["bash", "-n", "run-vlm-backfill-finalizer.sh"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_finalizer_installer_shell_syntax_is_valid():
    result = subprocess.run(
        ["bash", "-n", "install-launchd-vlm-backfill-finalizer.sh"], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_finalizer_prompt_encodes_full_safety_contract():
    prompt = Path("scripts/finalizer_handoff_prompt.md").read_text()
    for required in (
        "budget-router-backfill-20260707-14-v1",
        "240",
        "model_requested",
        "model_actual",
        "claude-sonnet-5",
        "com.petcam.vlm-historical-backfill",
        "com.petcam.vlm-candidate-worker",
        "com.petcam.activity-worker",
        "clip8",
        "storage/vlm-backfill-20260707-14",
        "git diff --check",
        "report_vlm_backfill.py",
        "reset --hard",
        "push --force",
        "clip_prelabels",
        "clip_activity_assessments",
        "behavior_labels",
    ):
        assert required in prompt, f"missing: {required}"


def test_finalizer_prompt_never_hardcodes_full_uuid_or_secret_pattern():
    prompt = Path("scripts/finalizer_handoff_prompt.md").read_text()
    assert "SUPABASE_SERVICE_ROLE_KEY=" not in prompt
    assert "sk-ant-" not in prompt
    assert "@gmail.com" not in prompt
