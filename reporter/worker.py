"""윈도우 워커 main (W4a 뼈대): 조회 → 활동 집계 → Slack. claude 없이 도는 리포트.

리포트 뼈대(활동량·활동시간대)는 motion_clips DB 만으로 산출(0 비용). W4b 에서 claude 샘플
행동 태깅(탈피·음수)을 얹는다. 돌고 죽는다 — launchd 가 윈도우(2h)마다 재실행(22/00/02/04시
분산 = claude 한도 피크 분할, 메모리 claude-subscription-quota-shared).
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from reporter import config, indexer, slack
from reporter.summarize import summarize_activity
from reporter.timewin import window_bounds

_KST = ZoneInfo("Asia/Seoul")


def run() -> int:
    now = datetime.now(_KST)
    start, end = window_bounds(now, config.WINDOW_HOURS)
    clips = indexer.list_clips_for_window(start, end)
    s = summarize_activity(clips)
    slack.post_slack(_format(s, now))
    return 0


def _format(s: dict, now: datetime) -> str:
    """활동 요약 → Slack 1카드. 순수 표현 함수(로직은 summarize_activity)."""
    if s["clip_count"] == 0:
        return f"🦎 최근 {config.WINDOW_HOURS}h: 활동 클립 없음 ({now:%m/%d %H:%M} KST)"
    peak = f"{s['peak_hour_kst']}시경 집중" if s["peak_hour_kst"] is not None else "활동 분산"
    top = sorted(s["hourly_kst"].items(), key=lambda kv: -kv[1])[:3]  # 상위 3 시간대
    dist = " ".join(f"{h}시:{n}" for h, n in top)
    return (
        f"🦎 최근 {config.WINDOW_HOURS}h 활동 요약 ({now:%m/%d %H:%M} KST)\n"
        f"· 활동 클립 {s['clip_count']}개 (~{s['active_minutes']}분)\n"
        f"· {peak} · 시간대(KST) {dist}"
    )


if __name__ == "__main__":
    sys.exit(run())
