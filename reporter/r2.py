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

from reporter import config


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
    """motion_clips.r2_key 로 mp4 GET → dest 저장. dest 부모 디렉토리 자동 생성."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    get_r2_client().download_file(config.R2_BUCKET, r2_key, str(dest))
    return dest
