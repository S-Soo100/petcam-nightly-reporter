"""R2(S3 호환) clip 다운로드. lab backend/r2_uploader.py 의 client 패턴 이식.

path-style 강제 = R2 wildcard cert(*.r2.cloudflarestorage.com)는 한 단계만 매치 →
기본 virtual-host style(bucket 을 서브도메인으로 붙임)은 SSL handshake 실패. path-style 로
endpoint 호스트명을 그대로 써서 cert 매치. s3v4 = R2 가 유일하게 지원하는 서명 버전.
lru_cache 싱글톤 = client 는 HTTP 세션+auth 객체라 매 호출 생성 시 커넥션풀 낭비.
"""
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from reporter import config


class R2SourceMissing(RuntimeError):
    """R2 object 부재(404/NoSuchKey) — 재시도해도 없음 = terminal.

    메시지에는 error code 만 담고 raw response/message/r2_key/secret 은 담지 않는다(로그 위생).
    """


class R2AccessDenied(RuntimeError):
    """R2 인증·권한 오류(403/AccessDenied) — terminal. secret/key 원문 미포함."""


# design §6 — object missing/인증 오류는 terminal, 나머지(timeout/429/5xx)는 retryable.
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_DENIED_CODES = frozenset({"403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"})


def _error_code(exc: ClientError) -> str:
    response = getattr(exc, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))


def classify_r2_client_error(exc: ClientError):
    """ClientError → typed 예외(R2SourceMissing/R2AccessDenied) or None(=transient, 재시도).

    typed 예외에는 code 만 담는다. raw message(패스워드/URL/key 섞일 수 있음)는 담지 않는다.
    """
    code = _error_code(exc)
    if code in _MISSING_CODES:
        return R2SourceMissing(f"r2 object missing (code={code})")
    if code in _DENIED_CODES:
        return R2AccessDenied(f"r2 access denied (code={code})")
    return None


@lru_cache(maxsize=1)
def get_r2_client():
    """싱글톤 boto3 S3 client (R2 endpoint 로 설정)."""
    return boto3.client(
        "s3",
        endpoint_url=config.R2_ENDPOINT,
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",  # R2 표준값 (AWS 와 달리 single-region 추상화)
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def download_clip(r2_key: str, dest: Path) -> Path:
    """motion_clips.r2_key 로 mp4 GET → dest 저장. dest 부모 디렉토리 자동 생성.

    404/NoSuchKey → R2SourceMissing, 403/AccessDenied → R2AccessDenied 로 변환(terminal).
    transient(timeout/429/5xx) 및 기타 ClientError 는 그대로 raise → worker 가 retryable 처리.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        get_r2_client().download_file(config.R2_BUCKET, r2_key, str(dest))
    except ClientError as e:
        typed = classify_r2_client_error(e)
        if typed is not None:
            raise typed from e
        raise  # transient/기타 → 원본 유지(worker 의 generic except 가 r2_download_failed retryable)
    return dest
