from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "install-launchd-gme.sh"
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


def _run(tmp_path, extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir()
    bin_path = _stub_bin(tmp_path)
    env = {"HOME": str(home), "PATH": f"{bin_path}:/usr/bin:/bin", "LAUNCHCTL_LOG": str(tmp_path / "launch.log")}
    env.update(extra)
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True), home


def test_installer_is_fail_closed_and_uses_60_second_one_shot(tmp_path):
    bad, home = _run(tmp_path / "bad", {"GME_ENABLED": "0", "GME_EXPECTED_HOST": HOST})
    assert bad.returncode != 0
    assert not list(home.rglob("*.plist"))
    good, home = _run(tmp_path / "good", {"GME_ENABLED": "1", "GME_EXPECTED_HOST": HOST})
    assert good.returncode == 0, good.stderr
    text = next(home.rglob("*.plist")).read_text()
    assert "com.petcam.gme-worker" in text
    assert "reporter.gme_worker" in text
    assert "<integer>60</integer>" in text
    assert "WorkingDirectory" in text
    assert "SUPABASE" not in text and "R2_SECRET" not in text
