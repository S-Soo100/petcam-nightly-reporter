"""정규 VLM night 의 read-only 수용 audit(설계 §12). DB 조회만 하고 write/R2/Claude/Slack 을
호출하지 않는다. Slack 전송은 DB outbox 가 없으므로 not_verifiable_from_db 로 명시한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from reporter import config
from reporter.vlm_backfill_selector import BACKFILL_SELECTOR_VERSION

KST = ZoneInfo("Asia/Seoul")
_STALE_MIN = 30


def _night_window_starts(date_str: str) -> list[datetime]:
    """date D 밤의 4개 window_start(KST): D 20:00 / D 22:00 / D+1 00:00 / D+1 02:00."""
    d = date.fromisoformat(date_str)
    base = datetime(d.year, d.month, d.day, 20, tzinfo=KST)
    return [base + timedelta(hours=2 * i) for i in range(4)]


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_backfill_producer(run_id) -> bool:
    return str(run_id or "").startswith("backfill-")


def audit_night(sb, date_str, *, now, expected_host=None,
                regular_selector=config.VLM_SELECTOR_VERSION,
                backfill_selector=BACKFILL_SELECTOR_VERSION,
                model_expected="claude-sonnet-5") -> dict:
    windows = _night_window_starts(date_str)
    night_start = windows[0].astimezone(timezone.utc)
    night_end = (windows[-1] + timedelta(hours=2)).astimezone(timezone.utc)
    rows = (sb.table("clip_vlm_jobs").select("*")
            .gte("window_start", night_start.isoformat()).lt("window_start", night_end.isoformat())
            .execute().data)
    regular = [r for r in rows if r.get("selector_version") == regular_selector]
    backfill = [r for r in rows if r.get("selector_version") == backfill_selector]

    observed = {_parse(r["window_start"]) for r in regular if r.get("window_start")}
    missing = [w.isoformat() for w in windows if w.astimezone(timezone.utc) not in observed]

    hosts = Counter(r.get("producer_host") for r in regular)
    per_window = defaultdict(lambda: defaultdict(int))
    for r in regular:
        per_window[r.get("window_start")][r.get("camera_id")] += 1
    over_cap = [{"window": ws, "camera": str(cam)[:8], "jobs": n}
                for ws, cams in per_window.items() for cam, n in cams.items() if n > config.VLM_MAX_PER_CAMERA_WINDOW]

    status_dist = Counter(r.get("status") for r in regular)
    open_rows = [r for r in regular if r.get("status") in ("queued", "failed_retryable")]
    stale = [r for r in open_rows if r.get("queued_at")
             and (now - _parse(r["queued_at"])) > timedelta(minutes=_STALE_MIN)]

    succeeded = [r for r in regular if r.get("status") == "succeeded"]
    model_dist = Counter(r.get("model_actual") for r in succeeded)
    model_mismatch = status_dist.get("held_model_mismatch", 0) + sum(
        1 for r in succeeded if r.get("model_actual") and r.get("model_actual") != model_expected)

    crossover = (sum(1 for r in regular if _is_backfill_producer(r.get("producer_run_id")))
                 + sum(1 for r in backfill if not _is_backfill_producer(r.get("producer_run_id"))))
    host_mismatch = expected_host is not None and any(h != expected_host for h in hosts if h is not None)
    cost = sum(float(r.get("cost_usd") or 0) for r in regular)

    violations = []
    if missing:
        violations.append("missing_windows")
    if host_mismatch:
        violations.append("host_mismatch")
    if over_cap:
        violations.append("over_camera_window_cap")
    if stale:
        violations.append("stale_open_over_30m")
    if model_mismatch:
        violations.append("model_mismatch")
    if crossover:
        violations.append("selector_crossover")

    return {
        "date": date_str,
        "expected_windows": [w.isoformat() for w in windows],
        "observed_windows": sorted(w.isoformat() for w in observed),
        "missing_windows": missing,
        "producer_hosts": {str(k): v for k, v in hosts.items()},
        "per_window_over_cap": over_cap,
        "status_distribution": dict(status_dist),
        "stale_open_over_30m": len(stale),
        "model_actual_distribution": {str(k): v for k, v in model_dist.items()},
        "model_mismatch": model_mismatch,
        "selector_crossover": crossover,
        "backfill_jobs_in_night": len(backfill),
        "provider": config.VLM_PROVIDER,
        "cost_usd": cost,
        "slack_delivery": "not_verifiable_from_db",
        "violations": violations,
        "all_pass": not violations,
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Read-only VLM night acceptance audit")
    parser.add_argument("--date", required=True, help="night start date YYYY-MM-DD (KST)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--expected-host", default=config.VLM_EXPECTED_HOST or None)
    args = parser.parse_args(argv)
    from supabase import create_client
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    result = audit_night(sb, args.date, now=datetime.now(timezone.utc), expected_host=args.expected_host)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"date={result['date']} pass={result['all_pass']} violations={result['violations']}")
        print(f"missing={result['missing_windows']} hosts={result['producer_hosts']} "
              f"stale={result['stale_open_over_30m']} model_mismatch={result['model_mismatch']} "
              f"crossover={result['selector_crossover']} cost=${result['cost_usd']:g}")
    return 0 if result["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
