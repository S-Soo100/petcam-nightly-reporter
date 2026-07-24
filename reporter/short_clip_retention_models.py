"""짧은 영상 retention 불변 모델 + rounding (설계 §4).

DB 정본(petcam-lab `926e5f6`)의 route/ fingerprint 계약을 얇게 미러한다. 원칙:
  - frozen dataclass — candidate/route/claim 은 DB 사실이라 mutate 금지.
  - DeletionClaim.repr 은 raw r2_key/lease_token 을 절대 담지 않는다(로그 위생, plan Step 2).
  - 표시 길이는 JavaScript `Math.round`(라벨링 웹과 동일) = floor(x+0.5). Python `round()`(banker)
    가 아니라 DB `floor(duration+0.5)` 와 정확히 일치해야 한다.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

# DB 정본 route allowlist(fn_record_short_clip_detection). 미지 값은 worker 로 흘리지 않는다.
ALLOWED_ROUTES = frozenset(
    {"candidate", "quarantined", "protected", "reused", "reused_restored", "ineligible"}
)


def round_display_seconds(duration_sec: float) -> int:
    """표시 초 = floor(duration_sec + 0.5) (JS Math.round 시맨틱, nonnegative). Python round() 금지.

    유한·비음수만 허용; NaN/inf/음수는 ValueError(불완전 metadata 를 표시값으로 굳히지 않음).
    """
    if not math.isfinite(duration_sec) or duration_sec < 0:
        raise ValueError("invalid_duration")
    return math.floor(duration_sec + 0.5)


def _require(row: dict, key: str):
    if key not in row or row[key] is None:
        raise ValueError(f"missing field: {key}")
    return row[key]


@dataclass(frozen=True, slots=True)
class ShortClipCandidate:
    """fn_list_short_clip_detection_candidates 한 행. worker 는 clip UUID·timestamp 만 record 로 넘긴다."""

    clip_id: str
    started_at: str
    duration_sec: float
    displayed_duration_sec: int

    @classmethod
    def from_row(cls, row: dict) -> "ShortClipCandidate":
        clip_id = _require(row, "clip_id")
        duration = float(_require(row, "duration_sec"))
        displayed = row.get("displayed_duration_sec")
        displayed = round_display_seconds(duration) if displayed is None else int(displayed)
        return cls(
            clip_id=str(clip_id),
            started_at=str(_require(row, "started_at")),
            duration_sec=duration,
            displayed_duration_sec=displayed,
        )


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """fn_record_short_clip_detection route. worker 는 route 로만 집계한다(exclusion_id/state 미노출)."""

    route: str

    @classmethod
    def from_row(cls, row: dict) -> "DetectionResult":
        route = row.get("route")
        if route not in ALLOWED_ROUTES:
            raise ValueError(f"unknown detection route: {route!r}")
        return cls(route=route)


@dataclass(frozen=True, slots=True)
class DeletionClaim:
    """fn_claim_short_clip_media_deletions 한 행. r2_key/lease_token 은 repr 비노출(field repr=False)."""

    exclusion_id: str
    clip_id: str
    r2_key: str = field(repr=False)
    lease_token: str = field(repr=False)

    @classmethod
    def from_row(cls, row: dict) -> "DeletionClaim":
        return cls(
            exclusion_id=str(_require(row, "exclusion_id")),
            clip_id=str(_require(row, "clip_id")),
            r2_key=str(_require(row, "r2_key")),
            lease_token=str(_require(row, "lease_token")),
        )

    def key_fingerprint(self) -> str:
        """complete 가 저장할 소문자 SHA-256 64-hex(r2_key). DB CHECK `^[0-9a-f]{64}$` 와 일치."""
        return hashlib.sha256(self.r2_key.encode("utf-8")).hexdigest()
