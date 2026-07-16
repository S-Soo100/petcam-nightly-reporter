"""install-launchd-activity.sh 계약 — fail-closed host guard + plist 무결성(§5.2).

실제 bootstrap 없이 임시 HOME + stub launchctl 로 render/lint 만 검증. hostname 은 실제값을
써서 expected host 일치/불일치 경로를 모두 탄다.
"""

import os
import plistlib
import subprocess
from pathlib import Path

SCRIPT = "install-launchd-activity.sh"


def _actual_host():
    return subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()


def _install_env(tmp_path, **over):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for cmd in ("uv", "launchctl"):
        (bin_dir / cmd).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / cmd).chmod(0o755)
    env = {
        **os.environ, "HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin:/sbin",
        "ACTIVITY_EXPECTED_HOST": _actual_host(),
    }
    env.update(over)
    return env


def _run(env):
    return subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True)


def _plist(tmp_path):
    return plistlib.loads(
        (tmp_path / "Library/LaunchAgents/com.petcam.activity-worker.plist").read_bytes()
    )


def test_activity_installer_renders_valid_plist(tmp_path):
    result = _run(_install_env(tmp_path))
    assert result.returncode == 0, result.stderr
    payload = _plist(tmp_path)
    assert payload["Label"] == "com.petcam.activity-worker"
    assert payload["ProgramArguments"][-1] == "reporter.activity_worker"
    assert payload["RunAtLoad"] is True
    assert payload["StartInterval"] == 3600
    assert payload["WorkingDirectory"] == str(Path(SCRIPT).resolve().parent)
    env = payload["EnvironmentVariables"]
    assert env["ACTIVITY_EXPECTED_HOST"] == _actual_host()
    assert env["ACTIVITY_POLICY_VERSION"] == "activity-v1"
    assert payload["StandardOutPath"].endswith(".log")


def test_activity_installer_aborts_when_expected_host_missing(tmp_path):
    assert _run(_install_env(tmp_path, ACTIVITY_EXPECTED_HOST="")).returncode != 0


def test_activity_installer_aborts_on_host_mismatch(tmp_path):
    assert _run(_install_env(tmp_path, ACTIVITY_EXPECTED_HOST="some-other-host")).returncode != 0


def test_activity_installer_source_lints_and_no_self_approval_no_secrets():
    t = Path(SCRIPT).read_text()
    assert "plutil -lint" in t
    assert "SUPABASE_SERVICE_ROLE_KEY" not in t
    assert "R2_SECRET_ACCESS_KEY" not in t
    assert "ANTHROPIC_API_KEY" not in t
    # 현재 hostname 을 expected 로 자동 복사하지 않는다(자기 승인 금지)
    assert 'ACTIVITY_EXPECTED_HOST="$ACTUAL_HOST"' not in t
