from reporter.gme_runtime_policy import allow_historical_claim


def test_backfill_pauses_when_live_lag_exceeds_15_minutes():
    assert allow_historical_claim({"oldest_live_age_sec": 901}, max_live_lag_sec=900) is False
    assert allow_historical_claim({"oldest_live_age_sec": 900}, max_live_lag_sec=900) is True


def test_malformed_stats_fail_closed_for_backfill():
    assert allow_historical_claim({}, max_live_lag_sec=900) is False
    assert allow_historical_claim({"oldest_live_age_sec": "unknown"}, max_live_lag_sec=900) is False
