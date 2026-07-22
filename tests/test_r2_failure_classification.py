"""B1R2 — R2 download 실패 분류 (404/403 terminal 분리, transient 유지).

design §6: HTTP 404/NoSuchKey → source_media_missing terminal, 403/AccessDenied → r2_access_denied
terminal, timeout/429/5xx → r2_download_failed retryable, 분류 불가 → r2_download_failed retryable.
typed 예외에는 raw response/key/secret 을 담지 않는다. 전체 경로(raw ClientError → r2.py 분류 →
worker 매핑 → (failure_code, retryable))를 실제 worker 로 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from botocore.exceptions import ClientError

import reporter.r2 as r2
from reporter.r2 import R2SourceMissing, classify_r2_client_error

# worker 매핑을 실제로 태우기 위해 worker 테스트의 Spies/헬퍼 재사용.
from tests.test_python_evidence_worker import Spies, _job, _run_jobs


def _client_error(code: str) -> ClientError:
    status = int(code) if code.isdigit() else 400
    return ClientError(
        {"Error": {"Code": code, "Message": "raw-secret-detail://user:pw@host/terra-clips/clips/x.mp4"},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


def raising_client_error(code: str):
    """실제 r2.py 분류기를 태워 typed(or raw) 예외를 던지는 download_fn 을 만든다."""
    exc = _client_error(code)

    def _dl(r2_key, dest):
        typed = classify_r2_client_error(exc)
        raise (typed if typed is not None else exc)

    return _dl


def run_failure(download_fn):
    """실제 worker.process_jobs 를 태워 (failure_code, retryable) 를 얻는다."""
    s = Spies()
    s.download = download_fn  # 주입: download 만 교체
    _run_jobs([_job()], {"clip-1": "k1"}, s)
    _job_id, code, retryable = s.calls["fail"][0]
    return (code, retryable)


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_missing_object_is_terminal(code):
    assert run_failure(raising_client_error(code)) == ("source_media_missing", False)


@pytest.mark.parametrize("code", ["403", "AccessDenied"])
def test_access_denied_is_terminal(code):
    assert run_failure(raising_client_error(code)) == ("r2_access_denied", False)


@pytest.mark.parametrize("code", ["429", "500", "503", "RequestTimeout"])
def test_transient_error_is_retryable(code):
    assert run_failure(raising_client_error(code)) == ("r2_download_failed", True)


def test_store_allowlist_matches_db_check_contract():
    """Python allowlist ↔ DB CHECK 1:1 고정. lab migration test 가 SQL 측 동일 집합을 pin 한다.

    양쪽이 같은 canonical 집합을 hardcode → 어느 쪽이 drift 나도 테스트가 깨진다(design §6).
    """
    from reporter.python_evidence_store import ALLOWED_FAILURE_CODES

    assert ALLOWED_FAILURE_CODES == frozenset({
        "r2_download_failed", "source_media_missing", "r2_access_denied",
        "decode_no_frames", "decode_insufficient_frames", "invalid_metadata",
        "detector_failed", "temporal_compute_failed", "db_transient", "db_error", "internal_error",
    })


def test_typed_exceptions_do_not_leak_key_or_secret():
    for code in ("404", "403"):
        typed = classify_r2_client_error(_client_error(code))
        assert typed is not None
        assert "raw-secret-detail" not in str(typed)
        assert "terra-clips" not in str(typed)


def test_download_clip_raises_typed_source_missing(monkeypatch):
    class FakeClient:
        def download_file(self, bucket, key, dest):
            raise _client_error("NoSuchKey")

    monkeypatch.setattr(r2, "get_r2_client", lambda: FakeClient())
    with pytest.raises(R2SourceMissing):
        r2.download_clip("terra-clips/clips/x.mp4", Path("/tmp/b1r2-nonexistent.mp4"))


def test_download_clip_reraises_transient(monkeypatch):
    class FakeClient:
        def download_file(self, bucket, key, dest):
            raise _client_error("503")

    monkeypatch.setattr(r2, "get_r2_client", lambda: FakeClient())
    # transient 은 typed 로 변환하지 않고 원본 ClientError 그대로(worker 가 retryable 처리)
    with pytest.raises(ClientError):
        r2.download_clip("terra-clips/clips/x.mp4", Path("/tmp/b1r2-nonexistent.mp4"))
