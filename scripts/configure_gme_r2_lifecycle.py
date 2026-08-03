"""R2 GME debug prefix에만 14일 lifecycle을 병합한다. 기본 dry-run."""

from __future__ import annotations

import argparse

from botocore.exceptions import ClientError

from reporter import config, r2

RULE_ID = "gme-debug-14d-v1"
DEBUG_PREFIX = "terra-derived/gme/v1/debug-14d/"


def merge_lifecycle_rules(existing: list[dict]) -> list[dict]:
    kept = [rule for rule in existing if rule.get("ID") != RULE_ID]
    kept.append({
        "ID": RULE_ID, "Status": "Enabled", "Filter": {"Prefix": DEBUG_PREFIX},
        "Expiration": {"Days": 14},
    })
    return kept


def _read_rules(client, bucket: str) -> list[dict]:
    try:
        return client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    except ClientError as exc:
        code = str((exc.response or {}).get("Error", {}).get("Code", ""))
        if code in {"NoSuchLifecycleConfiguration", "NoSuchLifecycle"}:
            return []
        raise RuntimeError(f"lifecycle read failed ({code or type(exc).__name__})") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="merge GME debug 14-day R2 lifecycle")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    client = r2.get_r2_client()
    existing = _read_rules(client, config.R2_BUCKET)
    merged = merge_lifecycle_rules(existing)
    if args.apply:
        client.put_bucket_lifecycle_configuration(
            Bucket=config.R2_BUCKET, LifecycleConfiguration={"Rules": merged}
        )
        verified = _read_rules(client, config.R2_BUCKET)
        if merge_lifecycle_rules(verified) != verified:
            print("[gme-lifecycle] verification failed")
            return 2
    print(f"[gme-lifecycle] existing={len(existing)} merged={len(merged)} apply={int(args.apply)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
