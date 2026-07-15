"""정규 candidate worker 배포 전 read-only preflight(§11 Gate C). Claude VLM 호출·DB write 를
하지 않는다. 출력은 PASS/FAIL code=<allowlist> 만 — secret 값(키/이메일/토큰)은 절대 출력하지 않는다.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta

from reporter.claude_cli_analyzer import CliBatchError, check_cli_auth

_REQUIRED_ENV = (
    "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "R2_ENDPOINT", "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "VLM_EXPECTED_HOST",
)
_EXACT_MODEL = "claude-sonnet-5"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    code: str  # allowlisted short reason, never a secret value


def _git(runner, *args):
    return runner(["git", *args], capture_output=True, text=True)


def run_preflight(*, env=None, hostname_fn=socket.gethostname, runner=subprocess.run,
                  which_fn=shutil.which, local_utcoffset_fn=None, tmp_write_fn=None) -> list[Check]:
    env = os.environ if env is None else env
    local_utcoffset_fn = local_utcoffset_fn or (lambda: datetime.now().astimezone().utcoffset())
    checks: list[Check] = []

    expected = (env.get("VLM_EXPECTED_HOST") or "").strip()
    actual = (hostname_fn() or "").strip()
    checks.append(Check("host", bool(expected) and actual == expected,
                        "ok" if expected and actual == expected else "host_mismatch"))

    branch = _git(runner, "rev-parse", "--abbrev-ref", "HEAD")
    on_main = branch.returncode == 0 and branch.stdout.strip() == "main"
    checks.append(Check("branch", on_main, "ok" if on_main else "not_main"))

    head = _git(runner, "rev-parse", "HEAD")
    origin = _git(runner, "rev-parse", "origin/main")
    synced = (head.returncode == 0 and origin.returncode == 0
              and head.stdout.strip() != "" and head.stdout.strip() == origin.stdout.strip())
    checks.append(Check("head_synced", synced, "ok" if synced else "head_unsynced"))

    missing = [name for name in _REQUIRED_ENV if not (env.get(name) or "").strip()]
    checks.append(Check("required_env", not missing, "ok" if not missing else "env_missing"))

    claude_path = which_fn("claude")
    checks.append(Check("claude_exe", bool(claude_path), "ok" if claude_path else "claude_missing"))

    try:
        check_cli_auth(runner=runner)  # raw output 은 파싱 후 버려 재출력하지 않는다
        auth_ok, auth_code = True, "ok"
    except CliBatchError as exc:
        auth_ok, auth_code = False, exc.code
    checks.append(Check("claude_auth", auth_ok, auth_code))

    model = (env.get("ANTHROPIC_MODEL_EXACT") or _EXACT_MODEL).strip()
    checks.append(Check("model", model == _EXACT_MODEL, "ok" if model == _EXACT_MODEL else "model_not_exact"))

    tz_ok = local_utcoffset_fn() == timedelta(hours=9)  # StartCalendarInterval 은 host local tz 를 따른다
    checks.append(Check("timezone", tz_ok, "ok" if tz_ok else "tz_not_kst"))

    checks.append(Check("temp_writable", *_temp_writable(tmp_write_fn)))
    return checks


def _temp_writable(tmp_write_fn):
    if tmp_write_fn is not None:
        ok = bool(tmp_write_fn())
        return ok, "ok" if ok else "temp_unwritable"
    try:
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"ok")
        return True, "ok"
    except OSError:
        return False, "temp_unwritable"


def build_payload(checks: list[Check]) -> dict:
    return {
        "host": socket.gethostname(),
        "all_pass": all(c.ok for c in checks),
        "checks": [{"name": c.name, "ok": c.ok, "code": c.code} for c in checks],
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # worker 와 동일하게 .env 를 로드해 required_env 를 실제 런타임 기준으로 검사.
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    checks = run_preflight()
    payload = build_payload(checks)
    if "--json" in argv:
        print(json.dumps(payload))
    else:
        for c in checks:
            print(f"{'PASS' if c.ok else 'FAIL'} {c.name} code={c.code}")
    return 0 if payload["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
