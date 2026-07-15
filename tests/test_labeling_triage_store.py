import pytest

from reporter.labeling_triage_models import TriageSuggestion
from reporter.labeling_triage_store import store_triage_suggestion


class _Rpc:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self


class _SB:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.calls = []

    def rpc(self, name, args):
        self.calls.append((name, args))
        return _Rpc(self.data, self.error)


def _suggestion():
    return TriageSuggestion(
        clip_id="clip",
        suggested_route="quarantine",
        suggestion_reason="gate_absent",
        suggestion_source="gate_activity_policy",
        policy_version="labeling-triage-v1",
        evidence_snapshot={"identity": "abc"},
    )


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"ok": True, "changed": True}, "stored"),
        ({"ok": True, "changed": False}, "reused"),
        ({"ok": False, "code": "labeling_started"}, "protected_session"),
    ],
)
def test_maps_rpc_results(payload, status):
    sb = _SB(payload)
    result = store_triage_suggestion(sb, _suggestion())
    assert result.status == status
    name, args = sb.calls[0]
    assert name == "fn_upsert_clip_labeling_triage_suggestion"
    assert set(args) == {
        "p_clip_id", "p_suggested_route", "p_suggestion_reason",
        "p_suggestion_source", "p_policy_version", "p_evidence_snapshot",
    }


def test_rpc_failure_is_not_reported_as_success():
    with pytest.raises(RuntimeError, match="db down"):
        store_triage_suggestion(_SB(error=RuntimeError("db down")), _suggestion())


def test_unknown_rpc_domain_code_raises():
    with pytest.raises(RuntimeError, match="unexpected triage RPC result"):
        store_triage_suggestion(_SB({"ok": False, "code": "other"}), _suggestion())
