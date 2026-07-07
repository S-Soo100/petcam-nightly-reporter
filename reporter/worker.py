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

from supabase import create_client

from reporter import classify, config, frames, indexer, r2, register, slack
from reporter.summarize import summarize_activity, summarize_behaviors
from reporter.timewin import window_bounds

_KST = ZoneInfo("Asia/Seoul")


def run() -> int:
    now = datetime.now(_KST)
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
    """활동(DB) + 행동(claude 샘플) → Slack 상황판 1카드. 순수 표현 함수."""
    win_min = int(config.WINDOW_HOURS * 60)
    if activity["clip_count"] == 0:
        return f"🦎 최근 {win_min}분: 활동 없음 ({now:%m/%d %H:%M} KST)"
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
    # 샘플 라벨 → 신호 우선순위: (1)claude 인프라 전량실패 경보 (2)특이행동 (3)게코 부재(unseen=
    # 분석불필요, gate v3 전 대체신호) (4)그 외(moving 등). '특이행동 없음' 과 '게코 안 보임' 을
    # 구분해야 조용한 한도실패도, 노이즈성 움직임도 리포트만 봐서 판별됨.
    failed = behaviors["failed_infra"]
    sampled = behaviors["sampled_count"]
    unseen = behaviors["unseen"]
    if sampled > 0 and failed >= sampled:
        sig = f"⚠️분석실패 {failed}/{sampled} (claude 한도/인증 — 로그 확인)"
    elif signals:
        sig = " ".join(signals) + (f" ⚠️일부실패 {failed}/{sampled}" if failed else "")
    elif sampled > 0 and unseen >= sampled:
        sig = "게코 안 보임(분석불필요)"
    else:
        sig = "특이행동 없음" + (f" ⚠️일부실패 {failed}/{sampled}" if failed else "")
    return (
        f"🦎 최근 {win_min}분 상황판 ({now:%m/%d %H:%M} KST)\n"
        f"· 움직임 {activity['clip_count']}회 (~{activity['active_minutes']}분)\n"
        f"· {peak} · 시간대(KST) {dist}\n"
        f"· 샘플{sampled}: {sig}"
    )


if __name__ == "__main__":
    sys.exit(run())
