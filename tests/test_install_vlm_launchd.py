import os
import plistlib
import subprocess
from pathlib import Path


def _actual_host():
    return subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()


def _install_env(tmp_path, **over):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for cmd in ("uv", "claude", "launchctl"):
        (bin_dir / cmd).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / cmd).chmod(0o755)
    env = {
        **os.environ, "HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin:/sbin",
        "VLM_ROUTER_ENABLED": "1", "VLM_PROVIDER": "claude_cli_batch",
        "ANTHROPIC_MODEL_EXACT": "claude-sonnet-5", "VLM_EXPECTED_HOST": _actual_host(),
    }
    env.update(over)
    return env


def _run(env):
    return subprocess.run(["bash", "install-launchd-vlm-candidate.sh"], env=env, capture_output=True, text=True)


def test_candidate_installer_renders_valid_plist(tmp_path):
    result = _run(_install_env(tmp_path))
    assert result.returncode == 0, result.stderr
    payload = plistlib.loads((tmp_path / "Library/LaunchAgents/com.petcam.vlm-candidate-worker.plist").read_bytes())
    assert sorted(e["Hour"] for e in payload["StartCalendarInterval"]) == [0, 2, 4, 22]
    assert all(e["Minute"] == 0 for e in payload["StartCalendarInterval"])
    env = payload["EnvironmentVariables"]
    assert env["VLM_ROUTER_ENABLED"] == "1"
    assert env["VLM_PROVIDER"] == "claude_cli_batch"
    assert env["ANTHROPIC_MODEL_EXACT"] == "claude-sonnet-5"
    assert env["REGISTER_HIGHLIGHTS"] == "0"
    assert env["VLM_EXPECTED_HOST"] == _actual_host()
    assert {"HOME", "USER", "LOGNAME", "PATH"}.issubset(env)
    assert "RunAtLoad" not in payload and "StartInterval" not in payload
    assert payload["StandardOutPath"].endswith(".log")


def test_installer_aborts_when_expected_host_missing(tmp_path):
    assert _run(_install_env(tmp_path, VLM_EXPECTED_HOST="")).returncode != 0


def test_installer_aborts_on_host_mismatch(tmp_path):
    assert _run(_install_env(tmp_path, VLM_EXPECTED_HOST="some-other-host")).returncode != 0


def test_installer_aborts_on_direct_api_provider(tmp_path):
    assert _run(_install_env(tmp_path, VLM_PROVIDER="direct_api")).returncode != 0


def test_installer_aborts_when_not_enabled(tmp_path):
    assert _run(_install_env(tmp_path, VLM_ROUTER_ENABLED="0")).returncode != 0


def test_installer_aborts_on_wrong_model(tmp_path):
    assert _run(_install_env(tmp_path, ANTHROPIC_MODEL_EXACT="claude-sonnet-4-6")).returncode != 0


def test_installer_aborts_when_claude_missing_from_path(tmp_path):
    env = _install_env(tmp_path)
    (tmp_path / "bin" / "claude").unlink()
    assert _run(env).returncode != 0


def test_installer_source_lints_before_bootstrap_and_has_no_secrets():
    t = Path("install-launchd-vlm-candidate.sh").read_text()
    assert "plutil -lint" in t
    assert "SUPABASE_SERVICE_ROLE_KEY" not in t
    assert "ANTHROPIC_API_KEY" not in t
    # 현재 hostname 을 expected 로 자동 복사하지 않는다(자기 승인 금지)
    assert 'VLM_EXPECTED_HOST="$ACTUAL_HOST"' not in t
