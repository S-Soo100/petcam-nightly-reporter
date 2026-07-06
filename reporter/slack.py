"""Slack Incoming Webhook 전송. 전송 주체는 워커(claude 아님) — 분리 진단."""
import httpx

from reporter import config


def post_slack(text: str) -> bool:
    """webhook 으로 1줄 전송. 실패해도 워커를 죽이지 않고 False 반환 + 로깅."""
    try:
        resp = httpx.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as e:
        print(f"[slack] FAIL: {e}")
        return False
