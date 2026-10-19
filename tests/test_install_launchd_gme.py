from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "install-launchd-gme.sh"
HOST = "petcam-macmini.local"
V26_SHA = "a00e5a7a1e1f9197accb036339a38a7c821f03c8ab79611ebce89e5cde59b513"
V26_FREEZE_SHA = "8f8e02beb452ec2ddfdce344dff507294f56136c69224990c50552d22bb343a0"
V26_IDENTITY = "deccfc8315d3c00edb5bf59db3c573dca568e9d6d7a5da8d7dc93d2082bdb899"
BETA_V2_SHA = "91a275ad1a047e7a0f70a8a9c1212da7d2b21532bb568a33b3401be9e338751d"
BETA_V2_FREEZE_SHA = "74d2dc854198b8f871005d591dc18bd5273d7de857ebcea11b4d32f60343c75f"
BETA_V2_IDENTITY = "8968c0546e4a86486eb56fb1ab89939da9aedabe3306131ffe8f052669ee0964"
BETA_V3_SHA = "f1046788e179f40e0e8a72af650848af3fa7b934c08e54e28a77930dccc0fb5e"
BETA_V3_FREEZE_SHA = "430c7f0485acd406fb8415603e8a135ab67213bb0a101cbb6bab0127fd5bd57c"
BETA_V3_IDENTITY = "83a36f6378aebc6e911deebc003d5307625703b33111babc2dd6d57dc502ff9e"


def _v26_env():
    return {
        "GME_ENABLED": "1",
        "GME_EXPECTED_HOST": HOST,
        "GME_BATCH_LIMIT": "10",
        "GME_DETECTOR_BACKEND": "yolo26n",
        "GME_CHECKPOINT_PATH": "/private/models/yolo26n-v26-best.pt",
        "GME_CHECKPOINT_SHA256": V26_SHA,
        "GME_DETECTOR_FREEZE_SHA256": V26_FREEZE_SHA,
        "GME_DETECTOR_IDENTITY": V26_IDENTITY,
        "GME_MODEL_VERSION": "v2.6-warm-start-s28",
        "GME_RAW_CONFIDENCE": "0.001",
        "GME_SCORE_THRESHOLD": "0.15",
        "GME_IMAGE_SIZE": "960",
        "GME_NMS_IOU": "0.70",
        "GME_POST_NMS_IOU": "0.55",
        "GME_MAX_DETECTIONS": "50",
        "GME_ANALYSIS_FPS": "10",
        "GME_ANCHOR_INTERVAL_SEC": "0.1",
        "GME_TEMPORAL_WINDOW_FRAMES": "5",
        "GME_TEMPORAL_MIN_POSITIVE_FRAMES": "3",
        "GME_DEVICE": "mps",
        "GME_ALLOWED_SOURCES": "smoke,live",
        "GME_SERVICE_LABEL": "com.petcam.gme-worker",
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
        _v26_env(),
    )
    assert good.returncode == 0, good.stderr
    text = next(home.rglob("*.plist")).read_text()
    assert "com.petcam.gme-worker" in text
    assert "reporter.gme_worker" in text
    assert "<integer>60</integer>" in text
    assert "<key>GME_BATCH_LIMIT</key><string>10</string>" in text
    assert "<key>GME_DETECTOR_BACKEND</key><string>yolo26n</string>" in text
    assert f"<key>GME_CHECKPOINT_SHA256</key><string>{V26_SHA}</string>" in text
    assert f"<key>GME_DETECTOR_FREEZE_SHA256</key><string>{V26_FREEZE_SHA}</string>" in text
    assert f"<key>GME_DETECTOR_IDENTITY</key><string>{V26_IDENTITY}</string>" in text
    assert "<key>GME_MODEL_VERSION</key><string>v2.6-warm-start-s28</string>" in text
    assert "<key>GME_RAW_CONFIDENCE</key><string>0.001</string>" in text
    assert "<key>GME_SCORE_THRESHOLD</key><string>0.15</string>" in text
    assert "<key>GME_IMAGE_SIZE</key><string>960</string>" in text
    assert "<key>GME_NMS_IOU</key><string>0.70</string>" in text
    assert "<key>GME_POST_NMS_IOU</key><string>0.55</string>" in text
    assert "<key>GME_MAX_DETECTIONS</key><string>50</string>" in text
    assert "<key>GME_ANALYSIS_FPS</key><string>10</string>" in text
    assert "<key>GME_ANCHOR_INTERVAL_SEC</key><string>0.1</string>" in text
    assert "<key>GME_TEMPORAL_WINDOW_FRAMES</key><string>5</string>" in text
    assert "<key>GME_TEMPORAL_MIN_POSITIVE_FRAMES</key><string>3</string>" in text
    assert "<key>GME_DEVICE</key><string>mps</string>" in text
    assert "<key>GME_ALLOWED_SOURCES</key><string>smoke,live</string>" in text
    assert "WorkingDirectory" in text
    assert "SUPABASE" not in text and "R2_SECRET" not in text


def test_installer_rejects_out_of_range_batch_limit(tmp_path):
    result, home = _run(
        tmp_path / "bad-batch",
        {**_v26_env(), "GME_BATCH_LIMIT": "51"},
    )
    assert result.returncode != 0
    assert not list(home.rglob("*.plist"))


def test_installer_rejects_missing_checkpoint_sha(tmp_path):
    env = _v26_env()
    del env["GME_CHECKPOINT_SHA256"]
    result, home = _run(tmp_path / "missing-sha", env)
    assert result.returncode != 0
    assert not list(home.rglob("*.plist"))


def test_installer_rejects_missing_detector_identity(tmp_path):
    env = _v26_env()
    del env["GME_DETECTOR_IDENTITY"]
    result, home = _run(tmp_path / "missing-identity", env)
    assert result.returncode != 0
    assert not list(home.rglob("*.plist"))


def test_installer_accepts_v261_and_rejects_mixed_contract(tmp_path):
    import plistlib
    env = {**_v26_env(),
        "GME_MODEL_VERSION": "v2.6.1-warm-start-s27",
        "GME_CHECKPOINT_SHA256": "313e933b623561027af9368244c16e8960c74d9b66ec1b919c8d3b58c0ff1c96",
        "GME_DETECTOR_FREEZE_SHA256": "a0c9d8305ee263a4302f7a0c0fc04a04e75b47b00815d91c088f56df42438bef",
        "GME_DETECTOR_IDENTITY": "44dd382cba74355c98eff3c202acf1b1195d92870c50ac86f4fafa4c75aed92e",
        "GME_SCORE_THRESHOLD": "0.30",
    }
    result, home = _run(tmp_path / "good", env)
    assert result.returncode == 0, result.stderr
    actual = plistlib.loads(next(home.rglob("*.plist")).read_bytes())["EnvironmentVariables"]
    assert actual["GME_MODEL_VERSION"] == "v2.6.1-warm-start-s27"
    for key in ("GME_MODEL_VERSION", "GME_CHECKPOINT_SHA256", "GME_DETECTOR_FREEZE_SHA256", "GME_DETECTOR_IDENTITY", "GME_SCORE_THRESHOLD"):
        result, home = _run(tmp_path / key, {**env, key: _v26_env()[key]})
        assert result.returncode != 0
        assert not list(home.rglob("*.plist"))


def _v261_env(**over):
    env = {**_v26_env(),
        "GME_MODEL_VERSION": "v2.6.1-warm-start-s27",
        "GME_CHECKPOINT_SHA256": "313e933b623561027af9368244c16e8960c74d9b66ec1b919c8d3b58c0ff1c96",
        "GME_DETECTOR_FREEZE_SHA256": "a0c9d8305ee263a4302f7a0c0fc04a04e75b47b00815d91c088f56df42438bef",
        "GME_DETECTOR_IDENTITY": "44dd382cba74355c98eff3c202acf1b1195d92870c50ac86f4fafa4c75aed92e",
        "GME_SCORE_THRESHOLD": "0.30",
    }
    env.update(over)
    return env


def test_installer_v2_with_015_identity_writes_algorithm_version(tmp_path):
    import plistlib
    env = _v261_env(GME_ALGORITHM_VERSION="gme-motion-v2", GME_SCORE_THRESHOLD="0.15",
                    GME_DETECTOR_IDENTITY="81688d0a5ae61299e569573bbc7429bbe362ef9f086cd1cf3e699f9c18c5de2c")
    result, home = _run(tmp_path / "v2", env)
    assert result.returncode == 0, result.stderr
    actual = plistlib.loads(next(home.rglob("*.plist")).read_bytes())["EnvironmentVariables"]
    assert actual["GME_ALGORITHM_VERSION"] == "gme-motion-v2"
    assert actual["GME_SCORE_THRESHOLD"] == "0.15"


def test_installer_defaults_algorithm_to_v1_and_rejects_unknown(tmp_path):
    import plistlib
    result, home = _run(tmp_path / "default", _v261_env())
    assert result.returncode == 0, result.stderr
    assert plistlib.loads(next(home.rglob("*.plist")).read_bytes())["EnvironmentVariables"]["GME_ALGORITHM_VERSION"] == "gme-motion-v1"
    result, home = _run(tmp_path / "bad", _v261_env(GME_ALGORITHM_VERSION="gme-motion-v9"))
    assert result.returncode != 0 and not list(home.rglob("*.plist"))


def test_installer_rejects_015_threshold_with_030_identity(tmp_path):
    result, home = _run(tmp_path / "mixed", _v261_env(GME_SCORE_THRESHOLD="0.15"))
    assert result.returncode != 0 and not list(home.rglob("*.plist"))


def test_installer_accepts_beta_v2_and_historical_service_contract(tmp_path):
    import plistlib
    env = {
        **_v26_env(),
        "GME_SERVICE_LABEL": "com.petcam.gme-historical-worker",
        "GME_ALLOWED_SOURCES": "historical",
        "GME_MODEL_VERSION": "v2.6.1-beta-v2-s27",
        "GME_CHECKPOINT_SHA256": BETA_V2_SHA,
        "GME_DETECTOR_FREEZE_SHA256": BETA_V2_FREEZE_SHA,
        "GME_DETECTOR_IDENTITY": BETA_V2_IDENTITY,
        "GME_SCORE_THRESHOLD": "0.80",
        "GME_ALGORITHM_VERSION": "gme-motion-v2",
    }
    result, home = _run(tmp_path / "historical", env)
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(next(home.rglob("*.plist")).read_bytes())
    assert plist["Label"] == "com.petcam.gme-historical-worker"
    assert plist["EnvironmentVariables"]["GME_ALLOWED_SOURCES"] == "historical"

    for key, value in {
        "GME_SERVICE_LABEL": "com.petcam.other",
        "GME_ALLOWED_SOURCES": "smoke,live,historical",
    }.items():
        result, bad_home = _run(tmp_path / key, {**env, key: value})
        assert result.returncode != 0
        assert not list(bad_home.rglob("*.plist"))


def test_installer_accepts_beta_v3_and_rejects_mixed_tuple(tmp_path):
    import plistlib
    env = {
        **_v26_env(),
        "GME_SERVICE_LABEL": "com.petcam.gme-historical-worker",
        "GME_ALLOWED_SOURCES": "historical",
        "GME_MODEL_VERSION": "v2.6.1-beta-v3-s27",
        "GME_CHECKPOINT_SHA256": BETA_V3_SHA,
        "GME_DETECTOR_FREEZE_SHA256": BETA_V3_FREEZE_SHA,
        "GME_DETECTOR_IDENTITY": BETA_V3_IDENTITY,
        "GME_SCORE_THRESHOLD": "0.70",
        "GME_ALGORITHM_VERSION": "gme-motion-v2",
    }
    result, home = _run(tmp_path / "v3", env)
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(next(home.rglob("*.plist")).read_bytes())
    assert plist["EnvironmentVariables"]["GME_DETECTOR_IDENTITY"] == BETA_V3_IDENTITY
    assert plist["EnvironmentVariables"]["GME_SCORE_THRESHOLD"] == "0.70"
    for key, value in {"GME_SCORE_THRESHOLD": "0.80", "GME_DETECTOR_IDENTITY": BETA_V2_IDENTITY,
                       "GME_CHECKPOINT_SHA256": BETA_V2_SHA, "GME_ALGORITHM_VERSION": "gme-motion-v1"}.items():
        result, bad_home = _run(tmp_path / f"bad-{key}", {**env, key: value})
        assert result.returncode != 0, key
        assert not list(bad_home.rglob("*.plist"))
