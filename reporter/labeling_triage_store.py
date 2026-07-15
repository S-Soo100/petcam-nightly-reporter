"""service-role triage suggestion RPC adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from reporter.labeling_triage_models import TriageSuggestion


@dataclass(frozen=True, slots=True)
class StoreResult:
    status: Literal["stored", "reused", "protected_session"]


def store_triage_suggestion(sb, suggestion: TriageSuggestion) -> StoreResult:
    args = {
        "p_clip_id": suggestion.clip_id,
        "p_suggested_route": suggestion.suggested_route,
        "p_suggestion_reason": suggestion.suggestion_reason,
        "p_suggestion_source": suggestion.suggestion_source,
        "p_policy_version": suggestion.policy_version,
        "p_evidence_snapshot": suggestion.evidence_snapshot,
    }
    data = sb.rpc("fn_upsert_clip_labeling_triage_suggestion", args).execute().data
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise RuntimeError("unexpected triage RPC result")
    if data.get("ok") is True:
        return StoreResult("stored" if data.get("changed") is True else "reused")
    if data.get("code") == "labeling_started":
        return StoreResult("protected_session")
    raise RuntimeError(f"unexpected triage RPC result: {data.get('code', 'invalid')}")
