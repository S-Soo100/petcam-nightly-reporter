"""GME 파생 artifact 전용 R2 writer. 원본 prefix PUT은 로컬에서 거부한다."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PERMANENT_PREFIX = "terra-derived/gme/v1/permanent/"
DEBUG_PREFIX = "terra-derived/gme/v1/debug-14d/"


class ArtifactUploadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UploadedArtifacts:
    permanent_key: str
    permanent_sha256: str
    permanent_bytes: int
    debug_key: str
    debug_sha256: str
    debug_bytes: int


def artifact_keys(clip_id: str, run_identity: str) -> tuple[str, str]:
    if not SAFE_COMPONENT.fullmatch(clip_id) or not SHA256.fullmatch(run_identity):
        raise ValueError("invalid artifact key component")
    filename = f"{run_identity}.json.gz"
    return f"{PERMANENT_PREFIX}{clip_id}/{filename}", f"{DEBUG_PREFIX}{clip_id}/{filename}"


def _put(client, *, bucket: str, key: str, body: bytes, digest: str) -> None:
    if not (key.startswith(PERMANENT_PREFIX) or key.startswith(DEBUG_PREFIX)):
        raise ValueError("artifact key outside GME prefixes")
    try:
        client.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/json",
            ContentEncoding="gzip", Metadata={"sha256": digest, "schema": "gme-artifact-v1"},
        )
    except Exception as exc:  # noqa: BLE001 - endpoint/key/credential 원문은 폐기한다.
        raise ArtifactUploadError(f"artifact upload failed ({type(exc).__name__})") from None


def upload_artifacts(client, *, bucket: str, clip_id: str, run_identity: str,
                     permanent: bytes, debug: bytes) -> UploadedArtifacts:
    if not bucket.strip() or not permanent or not debug:
        raise ValueError("bucket and artifact bodies are required")
    permanent_key, debug_key = artifact_keys(clip_id, run_identity)
    permanent_digest = hashlib.sha256(permanent).hexdigest()
    debug_digest = hashlib.sha256(debug).hexdigest()
    _put(client, bucket=bucket, key=permanent_key, body=permanent, digest=permanent_digest)
    _put(client, bucket=bucket, key=debug_key, body=debug, digest=debug_digest)
    return UploadedArtifacts(
        permanent_key, permanent_digest, len(permanent), debug_key, debug_digest, len(debug)
    )
