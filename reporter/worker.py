"""윈도우 워커 main: 조회 → 활동 집계(DB) → 샘플 행동 태깅(claude top-N) → Slack.

리포트 뼈대(활동량·시간대)는 motion_clips DB 로 산출(0 비용). 행동 종류(탈피·음수·급여)는
motion_score 상위 N개 clip만 claude 분류 — 전량은 clip당 ~12만 토큰이라 구독 한도 초과(W3 실측).
돌고 죽는다 — launchd 가 윈도우(2h)마다 재실행(22/00/02/04시 분산 = 한도 피크 분할).
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from reporter import classify, config, frames, indexer, r2, slack
from reporter.summarize import summarize_activity, summarize_behaviors
from reporter.timewin import window_bounds

_KST = ZoneInfo("Asia/Seoul")


def run() -> int:
    now = datetime.now(_KST)
    start, end = window_bounds(now, config.WINDOW_HOURS)
    clips = indexer.list_clips_for_window(start, end)
    activity = summarize_activity(clips)          # 뼈대: DB 만, 0 비용
    behaviors = _tag_sample(clips)                # 샘플: claude top-N
    ok = slack.post_slack(_format(activity, behaviors, now))
    # 매 실행 요약 1줄 — 성공/실패 무조건 로그로 흔적(관측성). 성공 경로가 조용해 로그가
    # 0바이트로 남는 바람에 '조용한 한도실패'를 며칠 못 챈 게 2026-07-07 근본원인.
    print(
        f"[worker] {now:%m-%d %H:%M} clips={activity['clip_count']} "
        f"sampled={behaviors['sampled_count']} ok={behaviors['analyzed_ok']} "
        f"fail={behaviors['failed_infra']} slack={'OK' if ok else 'FAIL'} "
        f"actions={behaviors['actions']}",
        flush=True,
    )
    return 0


def _tag_sample(clips) -> dict:
    """motion_score 상위 N개 clip 만 다운→프레임→claude 분류 → 행동 집계.

    clip 1개 실패(다운/추출/claude)가 윈도우 전체를 막지 않게 격리(action=error 로 계속).
    """
    top = sorted(clips, key=lambda c: c.motion_score, reverse=True)[:config.SAMPLE_TOP_N]
    labeled = []
    with tempfile.TemporaryDirectory() as tmp:
        for c in top:
            try:
                mp4 = r2.download_clip(c.r2_key, Path(tmp) / f"{c.id}.mp4")
                imgs = frames.extract_adaptive(mp4, Path(tmp) / c.id)
                label = classify.classify_clip(imgs)
            except Exception as e:  # noqa: BLE001 — clip 격리, 윈도우는 계속 진행
                print(f"[worker] clip {c.id[:8]} skip: {e}", file=sys.stderr)
                label = {"action": "error"}
            labeled.append(label)
    return summarize_behaviors(labeled)


def _format(activity: dict, behaviors: dict, now: datetime) -> str:
    """활동(DB) + 행동(claude 샘플) → Slack 1카드. 순수 표현 함수."""
    if activity["clip_count"] == 0:
        return f"🦎 최근 {config.WINDOW_HOURS}h: 활동 클립 없음 ({now:%m/%d %H:%M} KST)"
    peak = f"{activity['peak_hour_kst']}시경 집중" if activity["peak_hour_kst"] is not None else "활동 분산"
    top = sorted(activity["hourly_kst"].items(), key=lambda kv: -kv[1])[:3]  # 상위 3 시간대
    dist = " ".join(f"{h}시:{n}" for h, n in top)
    signals = []
    if behaviors["shed_observed"]:
        signals.append("🧬탈피")
    if behaviors["drink_observed"]:
        signals.append("💧음수")
    if behaviors["feeding_observed"]:
        signals.append("🍽급여")
    # claude 가 한도/인증으로 실패했는데 '특이행동 없음' 으로 나가면 정상과 구분이 안 됨(조용한
    # 실패). 전량 실패는 경보로 대체, 일부 실패는 꼬리표로 붙여 리포트만 봐도 즉시 인지되게.
    failed, sampled = behaviors["failed_infra"], behaviors["sampled_count"]
    if sampled > 0 and failed >= sampled:
        sig = f"⚠️분석실패 {failed}/{sampled} (claude 한도/인증 — 로그 확인)"
    else:
        sig = " ".join(signals) if signals else "특이행동 없음"
        if failed:
            sig += f" ⚠️일부실패 {failed}/{sampled}"
    return (
        f"🦎 최근 {config.WINDOW_HOURS}h 활동 요약 ({now:%m/%d %H:%M} KST)\n"
        f"· 활동 클립 {activity['clip_count']}개 (~{activity['active_minutes']}분)\n"
        f"· {peak} · 시간대(KST) {dist}\n"
        f"· 행동(상위{behaviors['sampled_count']}클립 샘플): {sig}"
    )


if __name__ == "__main__":
    sys.exit(run())
