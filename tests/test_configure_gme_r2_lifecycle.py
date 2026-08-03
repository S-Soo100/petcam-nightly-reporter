from scripts.configure_gme_r2_lifecycle import RULE_ID, merge_lifecycle_rules


def test_lifecycle_merge_preserves_existing_rules_and_adds_exact_debug_prefix():
    existing = [{"ID": "keep", "Status": "Enabled", "Filter": {"Prefix": "other/"}, "Expiration": {"Days": 30}}]
    merged = merge_lifecycle_rules(existing)
    assert merged[0] == existing[0]
    rule = next(row for row in merged if row["ID"] == RULE_ID)
    assert rule == {
        "ID": RULE_ID, "Status": "Enabled",
        "Filter": {"Prefix": "terra-derived/gme/v1/debug-14d/"},
        "Expiration": {"Days": 14},
    }


def test_lifecycle_merge_replaces_only_previous_gme_rule():
    existing = [
        {"ID": RULE_ID, "Status": "Disabled", "Filter": {"Prefix": "wrong/"}, "Expiration": {"Days": 1}},
        {"ID": "keep", "Status": "Enabled", "Filter": {"Prefix": "original/"}, "Expiration": {"Days": 99}},
    ]
    merged = merge_lifecycle_rules(existing)
    assert len(merged) == 2
    assert any(row["ID"] == "keep" for row in merged)
