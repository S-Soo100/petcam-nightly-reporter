"""install-launchd-short-clip-retention.sh — fail-closed 설치기 (temp HOME + stub, 실제 설치 없음).

expected host 미설정/불일치면 rendering/bootstrap 전에 거부. 일치하면 plist 를
$HOME/Library/LaunchAgents 에 쓰고 plutil -lint(bootstrap 이전) → stub launchctl bootstrap.
plist 는 short_clip_retention_worker 모듈·PATH·세 switch·StartInterval 3600·전용 로그경로를 담는다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "install-launchd-short-clip-retention.sh"
FAKE_HOST = "petcam-macmini.local"


def _stub_bin(tmp_path: Path) -> Path:
    b = tmp_path / "bin"
    b.mkdir()
    order = tmp_path / "order.log"
    (b / "launchctl").write_text(
        f'#!/usr/bin/env bash\necho "launchctl $*" >> "{order}"\nexit 0\n'
    )
    (b / "plutil").write_text(f'#!/usr/bin/env bash\necho "plutil $*" >> "{order}"\nexit 0\n')
    (b / "uv").write_text("#!/usr/bin/env bash\nexit 0\n")
    (b / "hostname").write_text(f"#!/usr/bin/env bash\necho {FAKE_HOST}\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return b


def _run(tmp_path, env_over):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    binp = _stub_bin(tmp_path)
    env = {"HOME": str(home), "PATH": f"{binp}:/usr/bin:/bin"}
    env.update(env_over)
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    return proc, home, tmp_path / "order.log"


def _agents(home: Path):
    return list((home / "Library" / "LaunchAgents").glob("*.plist"))


def test_script_exists_and_syntax_ok():
    assert SCRIPT.exists()
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_blank_expected_host_refused(tmp_path):
    proc, home, _ = _run(tmp_path, {"SHORT_CLIP_RETENTION_EXPECTED_HOST": ""})
    assert proc.returncode != 0
    assert _agents(home) == []


def test_hostname_mismatch_refused_before_write(tmp_path):
    proc, home, order = _run(tmp_path, {"SHORT_CLIP_RETENTION_EXPECTED_HOST": "other-host"})
    assert proc.returncode != 0
    assert _agents(home) == []  # plist 미작성
    assert not order.exists() or "launchctl" not in order.read_text()  # bootstrap 안 함


def test_installs_with_exact_plist_and_defaults(tmp_path):
    proc, home, order = _run(tmp_path, {"SHORT_CLIP_RETENTION_EXPECTED_HOST": FAKE_HOST})
    assert proc.returncode == 0, proc.stderr
    plists = _agents(home)
    assert len(plists) == 1
    content = plists[0].read_text()
    # label/module/working dir/interval/log 경로 정확.
    assert "com.petcam.short-clip-retention" in plists[0].name
    assert "<string>com.petcam.short-clip-retention</string>" in content
    assert "reporter.short_clip_retention_worker" in content
    assert "<key>WorkingDirectory</key>" in content
    assert "<key>StartInterval</key><integer>3600</integer>" in content
    assert "/tmp/short-clip-retention-worker.log" in content
    assert "<key>PATH</key>" in content
    # 세 switch 모두 렌더 + 기본값 enabled=1 / write=0 / delete=0.
    assert "<key>SHORT_CLIP_RETENTION_ENABLED</key><string>1</string>" in content
    assert "<key>SHORT_CLIP_RETENTION_WRITE_ENABLED</key><string>0</string>" in content
    assert "<key>SHORT_CLIP_RETENTION_DELETE_ENABLED</key><string>0</string>" in content
    assert f"<key>SHORT_CLIP_RETENTION_EXPECTED_HOST</key><string>{FAKE_HOST}</string>" in content


def test_plutil_lint_runs_before_bootstrap(tmp_path):
    proc, home, order = _run(tmp_path, {"SHORT_CLIP_RETENTION_EXPECTED_HOST": FAKE_HOST})
    assert proc.returncode == 0
    log = order.read_text()
    assert "plutil" in log and "bootstrap" in log
    assert log.index("plutil") < log.index("bootstrap")  # lint 가 bootstrap 이전


def test_output_prints_all_switches(tmp_path):
    proc, home, _ = _run(tmp_path, {"SHORT_CLIP_RETENTION_EXPECTED_HOST": FAKE_HOST})
    out = proc.stdout
    assert "SHORT_CLIP_RETENTION_ENABLED" in out
    assert "SHORT_CLIP_RETENTION_WRITE_ENABLED" in out
    assert "SHORT_CLIP_RETENTION_DELETE_ENABLED" in out


def test_expected_host_not_auto_copied(tmp_path):
    # expected host 를 안 주면(자동 승인 금지) 실제 hostname 이 있어도 설치하지 않는다.
    proc, home, _ = _run(tmp_path, {})  # EXPECTED_HOST 없음
    assert proc.returncode != 0
    assert _agents(home) == []
