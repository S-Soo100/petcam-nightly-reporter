from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "install-launchd-gme.sh"
HOST = "petcam-macmini.local"
V25_SHA = "2b128f105e898bc472ed66861583ab80007dae6e94b291db497d7a2f8081f84a"


def _v25_env():
    return {
        "GME_ENABLED": "1",
        "GME_EXPECTED_HOST": HOST,
        "GME_BATCH_LIMIT": "10",
        "GME_DETECTOR_BACKEND": "yolo26n",
        "GME_CHECKPOINT_PATH": "/private/models/yolo26n-v25-best.pt",
        "GME_CHECKPOINT_SHA256": V25_SHA,
        "GME_RAW_CONFIDENCE": "0.001",
        "GME_SCORE_THRESHOLD": "0.20",
        "GME_IMAGE_SIZE": "960",
        "GME_NMS_IOU": "0.70",
        "GME_MAX_DETECTIONS": "50",
        "GME_DEVICE": "mps",
    }


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
    good, home = _run(
        tmp_path / "good",
        _v25_env(),
    )
    assert good.returncode == 0, good.stderr
    text = next(home.rglob("*.plist")).read_text()
    assert "com.petcam.gme-worker" in text
    assert "reporter.gme_worker" in text
    assert "<integer>60</integer>" in text
    assert "<key>GME_BATCH_LIMIT</key><string>10</string>" in text
    assert "<key>GME_DETECTOR_BACKEND</key><string>yolo26n</string>" in text
    assert f"<key>GME_CHECKPOINT_SHA256</key><string>{V25_SHA}</string>" in text
    assert "<key>GME_RAW_CONFIDENCE</key><string>0.001</string>" in text
    assert "<key>GME_SCORE_THRESHOLD</key><string>0.20</string>" in text
    assert "<key>GME_IMAGE_SIZE</key><string>960</string>" in text
    assert "<key>GME_NMS_IOU</key><string>0.70</string>" in text
    assert "<key>GME_MAX_DETECTIONS</key><string>50</string>" in text
    assert "<key>GME_DEVICE</key><string>mps</string>" in text
    assert "WorkingDirectory" in text
    assert "SUPABASE" not in text and "R2_SECRET" not in text


def test_installer_rejects_out_of_range_batch_limit(tmp_path):
    result, home = _run(
        tmp_path / "bad-batch",
        {**_v25_env(), "GME_BATCH_LIMIT": "51"},
    )
    assert result.returncode != 0
    assert not list(home.rglob("*.plist"))


def test_installer_rejects_missing_checkpoint_sha(tmp_path):
    env = _v25_env()
    del env["GME_CHECKPOINT_SHA256"]
    result, home = _run(tmp_path / "missing-sha", env)
    assert result.returncode != 0
    assert not list(home.rglob("*.plist"))
