import json
from datetime import timedelta
from types import SimpleNamespace

from reporter.vlm_preflight import build_payload, run_preflight

_HOST = "mac-mini.verified"
_ENV = {
    "VLM_EXPECTED_HOST": _HOST, "SUPABASE_URL": "u", "SUPABASE_SERVICE_ROLE_KEY": "secret-key",
    "R2_ENDPOINT": "e", "R2_ACCESS_KEY_ID": "id", "R2_SECRET_ACCESS_KEY": "secret-r2",
    "R2_BUCKET": "b", "ANTHROPIC_MODEL_EXACT": "claude-sonnet-5",
}


def _runner(*, head="abc", origin="abc", branch="main", logged_in=True):
    def run(command, **_kwargs):
        if command[:2] == ["git", "rev-parse"]:
            if command[-1] == "--abbrev-ref" or "--abbrev-ref" in command:
                return SimpleNamespace(returncode=0, stdout=branch + "\n", stderr="")
            if command[-1] == "origin/main":
                return SimpleNamespace(returncode=0 if origin is not None else 1, stdout=(origin or "") + "\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=head + "\n", stderr="")
        if command[:3] == ["claude", "auth", "status"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": logged_in, "email": "acct@example.com"}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def _preflight(**over):
    kwargs = dict(env=_ENV, hostname_fn=lambda: _HOST, runner=_runner(),
                  which_fn=lambda _n: "/usr/local/bin/claude",
                  local_utcoffset_fn=lambda: timedelta(hours=9), tmp_write_fn=lambda: True)
    kwargs.update(over)
    return run_preflight(**kwargs)


def _codes(checks):
    return {c.name: (c.ok, c.code) for c in checks}


def test_all_checks_pass_and_payload_all_pass():
    checks = _preflight()
    assert all(c.ok for c in checks)
    assert build_payload(checks)["all_pass"] is True


def test_host_mismatch_fails_closed():
    checks = _preflight(hostname_fn=lambda: "macbook.local")
    assert _codes(checks)["host"] == (False, "host_mismatch")
    assert build_payload(checks)["all_pass"] is False


def test_missing_required_env_fails_without_leaking_values():
    env = dict(_ENV); env["SUPABASE_SERVICE_ROLE_KEY"] = ""
    checks = _preflight(env=env)
    assert _codes(checks)["required_env"][0] is False


def test_claude_not_logged_in_fails():
    checks = _preflight(runner=_runner(logged_in=False))
    assert _codes(checks)["claude_auth"][0] is False


def test_head_not_synced_fails_when_origin_unavailable():
    checks = _preflight(runner=_runner(origin=None))
    assert _codes(checks)["head_synced"] == (False, "head_unsynced")


def test_wrong_model_and_bad_timezone_fail():
    env = dict(_ENV); env["ANTHROPIC_MODEL_EXACT"] = "claude-sonnet-4-6"
    checks = _preflight(env=env, local_utcoffset_fn=lambda: timedelta(hours=0))
    codes = _codes(checks)
    assert codes["model"][0] is False and codes["timezone"][0] is False


def test_output_codes_never_contain_secret_values():
    checks = _preflight(runner=_runner(logged_in=False))
    blob = json.dumps(build_payload(checks))
    assert "secret-key" not in blob and "secret-r2" not in blob and "example.com" not in blob
