"""윈도우 워커 main: 조회 → 활동 집계(DB) → 샘플 행동 태깅(claude top-N) → Slack.

리포트 뼈대(활동량·시간대)는 motion_clips DB 로 산출(0 비용). 행동 종류(탈피·음수·급여)는
motion_score 상위 N개 clip만 claude 분류 — 전량은 clip당 ~12만 토큰이라 구독 한도 초과(W3 실측).
돌고 죽는다 — launchd 가 윈도우(2h)마다 재실행(22/00/02/04시 분산 = 한도 피크 분할).
"""
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from supabase import create_client

from reporter import classify, config, frames, indexer, r2, register, slack
from reporter.summarize import summarize_activity, summarize_behaviors
from reporter.timewin import window_bounds

_KST = ZoneInfo("Asia/Seoul")


def run() -> int:
    now = datetime.now(_KST)
    # 밤(20~06시 KST)만 분석 — SAMPLE_TOP_N↑ 하면 낮 A카메라 오탐이 claude 한도를 갉아먹어
    # 밤 예산이 줄어든다. 낮 창은 즉시 종료(claude 0). (2026-07-07 결정, config.NIGHT_ONLY 로 토글)
    if config.NIGHT_ONLY and not (now.hour >= 20 or now.hour < 6):
        print(f"[worker] {now:%m-%d %H:%M} skip(낮 — 밤만 분석)", flush=True)
        return 0
    start, end = window_bounds(now, config.WINDOW_HOURS)
    clips = indexer.list_clips_for_window(start, end)
    if not clips:
        # 활동 0 인 창은 상황판 스킵(빈 카드 스팸 방지) — 로그로만 흔적.
        print(f"[worker] {now:%m-%d %H:%M} clips=0 skip(no activity)", flush=True)
        return 0
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
    """motion_score 상위 N개 clip 만 다운→프레임→claude 분류 → 행동 집계 + 하이라이트 등록.

    clip 1개 실패(다운/추출/claude)가 윈도우 전체를 막지 않게 격리(action=error 로 계속).
    informative 라벨은 camera_clips+behavior_logs 로 자동 편입(앱 추론뷰·라벨링 큐 노출).
    등록 실패는 별도 격리 — 라벨/Slack 은 그대로(등록은 부가 기능).
    """
    top = sorted(clips, key=lambda c: c.motion_score, reverse=True)[:config.SAMPLE_TOP_N]
    # service_role client (RLS 우회 — camera_clips/behavior_logs 쓰기). 등록 off 면 생성 안 함.
    sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY) if config.REGISTER_HIGHLIGHTS else None
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
            _maybe_register(sb, c, label)
    return summarize_behaviors(labeled)


def _maybe_register(sb, clip, label: dict) -> None:
    """informative 라벨이면 하이라이트 등록(앱/라벨링 노출). 실패는 격리 — 리포트 흐름 불변."""
    if sb is None:
        return
    action = label.get("action", "")
    if not register.should_register(action):
        return
    try:
        status = register.register_highlight(
            sb, clip, action, label.get("confidence"), label.get("reasoning", "")
        )
        print(f"[worker] register {clip.id[:8]} {action} -> {status}", flush=True)
    except Exception as e:  # noqa: BLE001 — 등록 실패 격리, 리포트는 계속
        print(f"[worker] register {clip.id[:8]} FAIL: {e}", file=sys.stderr, flush=True)


def _format(activity: dict, behaviors: dict, now: datetime) -> str:
    """움직임 수집(DB) → Slack 요약 1카드. 순수 표현 함수. VLM 분석 결과가 아님을 명시.

    SAMPLE_TOP_N=0 배포에서는 legacy Claude 호출이 없어 행동 라벨이 없다. 방어적으로
    sampled>0 & 전량 실패면 조용한 한도실패 경보만 유지(2026-07-07 교훈).
    """
    if activity["clip_count"] == 0:
        return f"🦎 움직임 없음 ({now:%m/%d %H:%M} KST)"
    disp_end = now.replace(minute=0, second=0, microsecond=0)
    disp_start = disp_end - timedelta(hours=config.WINDOW_HOURS)
    peak = f"{activity['peak_hour_kst']}시" if activity["peak_hour_kst"] is not None else "분산"
    lines = [
        f"📊 움직임 수집 요약 ({disp_start:%H:%M}~{disp_end:%H:%M})",
        f"· 감지 클립 {activity['clip_count']}개 · 약 {activity['active_minutes']}분",
        f"· 집중 시간대: {peak}",
        "· 이 메시지는 움직임 수집 통계이며 VLM 분석 결과가 아님",
    ]
    sampled = behaviors["sampled_count"]
    failed = behaviors["failed_infra"]
    if sampled > 0 and failed >= sampled:
        lines.append(f"· ⚠️ 샘플 VLM 분석 전량 실패 {failed}/{sampled} (한도/인증 확인)")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(run())
