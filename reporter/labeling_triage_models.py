"""라벨링 격리 제안 worker의 불변 입력·출력 모델."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class LabelingTriageClip:
    id: str
    camera_id: str
    started_at: str
    duration_sec: float
    r2_key: str


@dataclass(frozen=True, slots=True)
class TriageSuggestion:
    clip_id: str
    suggested_route: Literal["label", "quarantine"]
    suggestion_reason: Literal["gate_active", "gate_absent", "gate_static"]
    suggestion_source: str
    policy_version: str
    evidence_snapshot: dict
