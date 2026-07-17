"""install-launchd-python-evidence.sh — fail-closed 설치기 (temp HOME + stub launchctl).

실제 설치/부팅 없이: expected host 미설정/불일치면 거부, enabled 아니면 거부, 일치하면 plist 를
$HOME/Library/LaunchAgents 에 쓰고 stub launchctl bootstrap 을 호출한다. plist 는 python_evidence_worker
모듈·PATH·expected host 를 담아야 한다.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install-launchd-python-evidence.sh"
FAKE_HOST = "petcam-macmini.local"


def _stub_bin(tmp_path: Path) -> Path:
    """launchctl/plutil/uv/hostname 를 stub 으로 채운 PATH 디렉토리."""
    b = tmp_path / "bin"
    b.mkdir()
    (b / "launchctl").write_text("#!/usr/bin/env bash\necho \"launchctl $*\" >> \"$LAUNCHCTL_LOG\"\nexit 0\n")
    (b / "plutil").write_text("#!/usr/bin/env bash\nexit 0\n")  # lint 항상 통과
    (b / "uv").write_text("#!/usr/bin/env bash\nexit 0\n")
    (b / "hostname").write_text(f"#!/usr/bin/env bash\necho {FAKE_HOST}\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return b


def _run(tmp_path, env_over):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    binp = _stub_bin(tmp_path)
    env = {
        "HOME": str(home),
        "PATH": f"{binp}:/usr/bin:/bin",
        "LAUNCHCTL_LOG": str(tmp_path / "launchctl.log"),
    }
    env.update(env_over)
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    return proc, home


def test_script_exists_and_syntax_ok():
    assert SCRIPT.exists()
    # bash -n 문법 검증
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_missing_expected_host_refused(tmp_path):
    proc, home = _run(tmp_path, {"PYTHON_EVIDENCE_ENABLED": "1", "PYTHON_EVIDENCE_EXPECTED_HOST": ""})
    assert proc.returncode != 0
    assert not list((home / "Library" / "LaunchAgents").iterdir())  # plist 미작성


def test_not_enabled_refused(tmp_path):
    proc, home = _run(tmp_path, {"PYTHON_EVIDENCE_ENABLED": "0", "PYTHON_EVIDENCE_EXPECTED_HOST": FAKE_HOST})
    assert proc.returncode != 0
    assert not list((home / "Library" / "LaunchAgents").iterdir())


def test_hostname_mismatch_refused(tmp_path):
    proc, home = _run(tmp_path, {"PYTHON_EVIDENCE_ENABLED": "1", "PYTHON_EVIDENCE_EXPECTED_HOST": "other-host"})
    assert proc.returncode != 0
    assert not list((home / "Library" / "LaunchAgents").iterdir())


def test_installs_plist_when_host_matches(tmp_path):
    proc, home = _run(tmp_path, {"PYTHON_EVIDENCE_ENABLED": "1", "PYTHON_EVIDENCE_EXPECTED_HOST": FAKE_HOST})
    assert proc.returncode == 0, proc.stderr
    plists = list((home / "Library" / "LaunchAgents").glob("*.plist"))
    assert len(plists) == 1
    content = plists[0].read_text()
    assert "reporter.python_evidence_worker" in content
    assert FAKE_HOST in content
    assert "PYTHON_EVIDENCE_EXPECTED_HOST" in content
    assert "PYTHON_EVIDENCE_ENABLED" in content
    assert "<key>PATH</key>" in content
    # bootstrap 호출됨
    log = (tmp_path / "launchctl.log").read_text()
    assert "bootstrap" in log
