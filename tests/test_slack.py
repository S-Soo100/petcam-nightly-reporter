"""reporter.slack.post_slack — 실패 시 raw HTTP 오류 원문(URL/token/response)을 출력하지 않는다.

예외 타입 + 안전한 상태코드만. 실제 네트워크 0(httpx.post monkeypatch)."""

from __future__ import annotations

import httpx

from reporter import config, slack


def _webhook(monkeypatch):
    monkeypatch.setattr(config, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/T0/B0/secret-token-xyz")


class _OkResp:
    def raise_for_status(self):
        return None


def test_post_slack_success(monkeypatch):
    _webhook(monkeypatch)
    monkeypatch.setattr(slack.httpx, "post", lambda *a, **k: _OkResp())
    assert slack.post_slack("hi") is True


def test_post_slack_request_error_hides_raw(monkeypatch, capsys):
    _webhook(monkeypatch)

    def boom(*a, **k):
        raise httpx.ConnectError("connect fail to https://hooks.slack.com/T0/B0/secret-token-xyz")

    monkeypatch.setattr(slack.httpx, "post", boom)
    assert slack.post_slack("hi") is False
    out = capsys.readouterr().out
    assert "secret-token-xyz" not in out and "hooks.slack.com" not in out
    assert "ConnectError" in out  # 예외 타입만


def test_post_slack_status_error_shows_only_status_code(monkeypatch, capsys):
    _webhook(monkeypatch)
    req = httpx.Request("POST", "https://hooks.slack.com/T0/B0/secret-token-xyz")
    resp = httpx.Response(500, request=req)

    class _BadResp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "500 Server Error for url https://hooks.slack.com/T0/B0/secret-token-xyz",
                request=req,
                response=resp,
            )

    monkeypatch.setattr(slack.httpx, "post", lambda *a, **k: _BadResp())
    assert slack.post_slack("hi") is False
    out = capsys.readouterr().out
    assert "secret-token-xyz" not in out and "hooks.slack.com" not in out
    assert "500" in out and "HTTPStatusError" in out
