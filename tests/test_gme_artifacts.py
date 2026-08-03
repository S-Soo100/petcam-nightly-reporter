from __future__ import annotations

import hashlib

import pytest

from reporter.gme_artifacts import ArtifactUploadError, artifact_keys, upload_artifacts


class Client:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_on and self.fail_on in kwargs["Key"]:
            raise RuntimeError("secret endpoint")


def test_keys_are_exact_and_reject_traversal():
    permanent, debug = artifact_keys("clip-1", "b" * 64)
    assert permanent == "terra-derived/gme/v1/permanent/clip-1/" + "b" * 64 + ".json.gz"
    assert debug == "terra-derived/gme/v1/debug-14d/clip-1/" + "b" * 64 + ".json.gz"
    with pytest.raises(ValueError):
        artifact_keys("../clips/secret", "b" * 64)
    with pytest.raises(ValueError):
        artifact_keys("clip-1", "short")


def test_upload_writes_only_approved_prefixes_with_digest_metadata():
    client = Client()
    permanent = b"permanent"
    debug = b"debug"
    result = upload_artifacts(client, bucket="bucket", clip_id="clip-1", run_identity="c" * 64,
                              permanent=permanent, debug=debug)
    assert [c["Key"].split("/")[3] for c in client.calls] == ["permanent", "debug-14d"]
    assert client.calls[0]["ContentEncoding"] == "gzip"
    assert client.calls[0]["Metadata"]["sha256"] == hashlib.sha256(permanent).hexdigest()
    assert result.permanent_bytes == len(permanent)


def test_upload_error_is_redacted_and_classified():
    client = Client(fail_on="debug-14d")
    with pytest.raises(ArtifactUploadError) as error:
        upload_artifacts(client, bucket="bucket", clip_id="clip-1", run_identity="d" * 64,
                         permanent=b"p", debug=b"d")
    assert "secret endpoint" not in str(error.value)
