import os
import plistlib
import subprocess
from pathlib import Path


def _run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for cmd in ("uv", "claude", "ffmpeg", "launchctl"):
        (bin_dir / cmd).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / cmd).chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    return subprocess.run(["bash", "install-launchd.sh"], env=env, capture_output=True, text=True)


def test_movement_installer_renders_2h_night_calendar_and_disables_claude(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    payload = plistlib.loads((tmp_path / "Library/LaunchAgents/com.petcam.nightly-reporter.plist").read_bytes())
    assert payload["ProgramArguments"][-1] == "reporter.worker"
    assert "RunAtLoad" not in payload and "StartInterval" not in payload
    hours = sorted(e["Hour"] for e in payload["StartCalendarInterval"])
    assert hours == [0, 2, 4, 22]
    assert all(e["Minute"] == 5 for e in payload["StartCalendarInterval"])  # 정규 VLM(:00)과 분리
    env = payload["EnvironmentVariables"]
    assert env["WINDOW_HOURS"] == "2"
    assert env["SAMPLE_TOP_N"] == "0"  # legacy Claude 차단 = candidate 중복 호출 방지


def test_movement_installer_source_has_no_30min_interval():
    t = Path("install-launchd.sh").read_text()
    assert "<key>StartInterval</key>" not in t
    assert "<key>StartCalendarInterval</key>" in t
