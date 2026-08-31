from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "install-launchd-yolo-http-worker.sh"
HOST = "petcam-macmini.local"


def _stub_bin(tmp_path):
    path = tmp_path / "bin"
    path.mkdir()
    for name, body in {
        "launchctl": '#!/usr/bin/env bash\necho "$*" >> "$LAUNCHCTL_LOG"\n',
        "plutil": "#!/usr/bin/env bash\nexit 0\n",
        "uv": "#!/usr/bin/env bash\nexit 0\n",
        "hostname": f"#!/usr/bin/env bash\necho {HOST}\n",
    }.items():
        file = path / name
        file.write_text(body)
        file.chmod(0o755)
    return path


def _run(tmp_path, *, env_mode="600", token="worker-token"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    script = repo / SCRIPT.name
    script.write_bytes(SCRIPT.read_bytes())
    script.chmod(0o755)
    env_file = repo / ".env"
    env_file.write_text(f"YOLO_HTTP_WORKER_TOKEN={token}\n")
    env_file.chmod(int(env_mode, 8))
    bin_path = _stub_bin(tmp_path)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_path}:/usr/bin:/bin",
        "LAUNCHCTL_LOG": str(tmp_path / "launch.log"),
        "YOLO_HTTP_EXPECTED_HOST": HOST,
    }
    return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True), home


def test_installer_uses_localhost_and_keeps_token_out_of_plist(tmp_path):
    result, home = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    text = next(home.rglob("*.plist")).read_text()
    assert "com.petcam.yolo-http-worker" in text
    assert "reporter.yolo_http_worker:app" in text
    assert "127.0.0.1" in text and "8765" in text
    assert "worker-token" not in text
    assert "WorkingDirectory" in text


def test_installer_rejects_group_readable_env_before_plist_write(tmp_path):
    result, home = _run(tmp_path, env_mode="640")

    assert result.returncode != 0
    assert not list(home.rglob("*.plist"))
