"""exact-object R2 삭제 adapter 계약 테스트 (설계 §7). 실제 R2 호출 0 — 전부 mock.

terra-clips/clips/<filename> 만 허용. list/bulk/prefix delete 금지. raw key/endpoint/응답 비노출.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

import reporter.r2 as r2
from reporter import config


class _FakeClient:
    def __init__(self, error=None):
        self.calls: list[tuple] = []
        self.error = error

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        if self.error is not None:
            raise self.error
        return {}

    def list_objects_v2(self, **kwargs):  # noqa: D401 — 절대 호출되면 안 됨
        self.calls.append(("list_objects_v2", kwargs))
        raise AssertionError("list called")

    def delete_objects(self, **kwargs):
        self.calls.append(("delete_objects", kwargs))
        raise AssertionError("bulk delete called")


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(r2, "get_r2_client", lambda: client)
    monkeypatch.setattr(config, "R2_BUCKET", "terra-clips")
    return client


def test_delete_calls_delete_object_exactly_once(fake_client):
    r2.delete_clip_object("terra-clips/clips/exact.mp4")
    assert fake_client.calls == [
        ("delete_object", {"Bucket": "terra-clips", "Key": "terra-clips/clips/exact.mp4"})
    ]


def test_delete_never_lists_or_bulk_deletes(fake_client):
    r2.delete_clip_object("terra-clips/clips/a.mp4")
    assert {c[0] for c in fake_client.calls} == {"delete_object"}


def test_delete_rejects_unsafe_keys(fake_client):
    for bad in (
        "",
        "   ",
        "/terra-clips/clips/x.mp4",           # leading slash
        "terra-clips/clips/x.mp4/",           # trailing slash
        "terra-clips/clips/../x.mp4",         # ..
        "terra-clips/clips/",                 # bare prefix
        "terra-clips/clips",                  # prefix without slash
        "clips/x.mp4",                        # outside prefix
        "other/clips/x.mp4",                  # outside prefix
        "terra-clips/clips/sub/x.mp4",        # nested path (filename only)
    ):
        with pytest.raises(ValueError):
            r2.delete_clip_object(bad)
    assert fake_client.calls == []  # 검증 실패 = R2 호출 0


def test_delete_r2_error_raises_without_raw_secret(monkeypatch):
    err = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "s3://user:pw@endpoint/terra-clips/clips/x.mp4"}},
        "DeleteObject",
    )
    client = _FakeClient(error=err)
    monkeypatch.setattr(r2, "get_r2_client", lambda: client)
    monkeypatch.setattr(config, "R2_BUCKET", "terra-clips")
    with pytest.raises(Exception) as ei:
        r2.delete_clip_object("terra-clips/clips/x.mp4")
    text = str(ei.value)
    assert "pw@endpoint" not in text and "user:pw" not in text
