"""Slack Incoming Webhook 전송. 전송 주체는 워커(claude 아님) — 분리 진단."""
import httpx

from reporter import config


def post_slack(text: str) -> bool:
    """webhook 으로 1줄 전송. 실패해도 워커를 죽이지 않고 False 반환.

    실패 로그에는 raw HTTP 오류 원문(webhook URL/token/response body)을 절대 담지 않는다 —
    예외 타입 + (HTTP 상태 오류면) 안전한 상태코드 정수만 출력한다(로그 위생).
    """
    try:
        resp = httpx.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as e:
        print(f"[slack] FAIL type=HTTPStatusError status={e.response.status_code}")
        return False
    except httpx.HTTPError as e:
        print(f"[slack] FAIL type={type(e).__name__}")
        return False
