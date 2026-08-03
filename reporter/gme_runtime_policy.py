"""live 지연이 커질 때 historical backfill만 멈추는 순수 운영 규칙."""

from __future__ import annotations

import math


def allow_historical_claim(stats: dict, *, max_live_lag_sec: float) -> bool:
    value = stats.get("oldest_live_age_sec")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        return False
    return value <= max_live_lag_sec
