"""백로그 백필 — 임의 시간창 clip 을 claude(v4.0 sonnet)로 분류(전수 배치·재개 지원).

자동화(launchd worker)는 '직전 윈도우'만 처리 → 이미 쌓인 과거 클립은 사각지대.
이 스크립트로 과거 밤을 조각내 수동 분류(맥북/맥미니 실행, claude 한도 통제 위해 배치로).

reporter 모듈 재사용: indexer(조회) · r2(다운로드) · frames(적응형 추출) · classify(claude).
Slack 안 보냄 — 콘솔 출력만(파일럿/검증 모드). worker 와 분리.

전수 배치·재개:
- --sort started_at 으로 시간순(맨 앞=오래된) 정렬, --top-n 으로 이번 배치 크기.
- --out 에 각 clip 즉시 기록 → 한도로 중단돼도 재실행 시 done skip 하고 다음 배치 이어서.
  단 action=error(claude 호출 실패=한도/인증)는 기록 안 함 → 재개 때 재시도(구멍 방지).
- --camera ALL 로 두 카메라 통합.
- --night-only 로 밤(20~06시 KST)만 — 여러 밤에 걸친 범위에서 낮 오탐 제외(worker 대상과 일치).

예:
  uv run python scripts/backfill_window.py --dry-run                          # 목록만(claude 0)
  uv run python scripts/backfill_window.py --camera ALL --sort started_at --night-only \\
    --start 2026-07-03T20:00:00+09:00 --end 2026-07-05T06:00:00+09:00 \\
    --top-n 300 --out storage/backfill/backlog.jsonl                          # 다음 300개, 중단 시 재실행으로 재개
"""
import argparse
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 레포 루트 → reporter import

from reporter import classify, frames, indexer, r2  # noqa: E402

# camera_id UUID 별칭 (motion_clips 실측: A=상시감지 / B=야행성 신호 깨끗)
CAMERAS = {
    "A": "5b3ea7aa-b4a7-4146-8f48-caf69e29e49c",
    "B": "f6599924-d133-4562-a48c-a06ff59db29d",
}
_FEEDING = {"eating_paste", "eating_prey", "hand_feeding"}
_KST = ZoneInfo("Asia/Seoul")


def _kst(iso_utc: str) -> str:
    """started_at(UTC ISO) → 'MM-DD HH:MM' KST 표기(출력용)."""
    return datetime.fromisoformat(iso_utc).astimezone(_KST).strftime("%m-%d %H:%M")


def _hour_kst(iso_utc: str) -> int:
    return datetime.fromisoformat(str(iso_utc)).astimezone(_KST).hour


def _all_clips_in(start: datetime, end: datetime):
    """넓은 창은 Supabase 기본 limit(1000)에 걸려 잘리니, 12h 씩 잘라 조회·합친다(하루도 1000 초과 사례 있음)."""
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(hours=12), end)
        out.extend(indexer.list_clips_for_window(cur, nxt))
        cur = nxt
    return out


def select_clips(start: datetime, end: datetime, camera_id: str, sort_key: str, night_only: bool):
    """[start, end) 창 조회 → camera 필터(ALL=전체) → (옵션)밤만 → 정렬. 전수 반환(배치 슬라이스는 호출측)."""
    clips = _all_clips_in(start, end)
    if camera_id != "ALL":
        clips = [c for c in clips if c.camera_id == camera_id]
    if night_only:
        clips = [c for c in clips if _hour_kst(c.started_at) >= 20 or _hour_kst(c.started_at) < 6]
    if sort_key == "started_at":
        clips.sort(key=lambda c: str(c.started_at))  # asc = 오래된 것부터(맨 앞)
    else:
        key = (lambda c: c.motion_score) if sort_key == "motion_score" else (lambda c: c.duration_sec)
        clips.sort(key=key, reverse=True)
    return clips


def main() -> int:
    ap = argparse.ArgumentParser(description="백로그 백필 분류(과거 밤 조각 처리·전수 배치 재개)")
    ap.add_argument("--start", default="2026-07-04T20:00:00+09:00", help="ISO8601(권장: +09:00 KST)")
    ap.add_argument("--end", default="2026-07-05T08:00:00+09:00")
    ap.add_argument("--camera", default="B", help="A/B/ALL 별칭 또는 camera_id UUID")
    ap.add_argument("--top-n", type=int, default=10, help="이번 배치 크기(재개 시 남은 것 중 앞 N개)")
    ap.add_argument("--sort", default="motion_score", choices=["motion_score", "duration", "started_at"])
    ap.add_argument("--night-only", action="store_true", help="밤(20~06시 KST)만 — 여러 밤 범위에서 낮 제외")
    ap.add_argument("--out", type=Path, help="결과 jsonl 저장(등록 입력 겸 재개 상태). error 는 미기록→재개 시 재시도")
    ap.add_argument("--dry-run", action="store_true", help="이번 배치 목록만 출력(claude 호출 0)")
    args = ap.parse_args()

    start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
    camera_id = CAMERAS.get(args.camera.upper(), args.camera)
    cam_label = args.camera.upper() if args.camera.upper() in CAMERAS else args.camera.upper()[:8]

    # 전수 정렬 → done(이미 기록된 성공분) 제외 → 남은 것 앞 top_n = 이번 배치(재개 정확).
    all_clips = select_clips(start, end, camera_id, args.sort, args.night_only)
    done_ids: set[str] = set()
    if args.out and args.out.exists():
        done_ids = {json.loads(x)["clip_id"] for x in args.out.read_text().splitlines() if x.strip()}
    remaining = [c for c in all_clips if c.id not in done_ids]
    top = remaining[: args.top_n]

    scope = " · 밤만" if args.night_only else ""
    print(f"[백필] 카메라 {cam_label} · {args.start} ~ {args.end} · {args.sort}{scope}")
    print(f"  전수 {len(all_clips)} · 완료 {len(done_ids)} · 남은 {len(remaining)} → 이번 배치 {len(top)}개")
    for i, c in enumerate(top, 1):
        print(f"  {i:3d}. {_kst(c.started_at)}  score={c.motion_score:.4f}  dur={c.duration_sec:.0f}s  {c.id[:8]}")

    if args.dry_run:
        print("[dry-run] claude 호출 없이 종료.")
        return 0
    if not top:
        print("[백필] 이번 배치 0건(전부 완료됐거나 대상 없음) — 종료.")
        return 0

    print(f"\n[백필] claude(v4.0 sonnet) 분류 시작 — {len(top)}회 호출(한도 소모)...\n")
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for i, c in enumerate(top, 1):
            try:
                mp4 = r2.download_clip(c.r2_key, tmp_dir / f"{c.id}.mp4")
                imgs = frames.extract_adaptive(mp4, tmp_dir / c.id)
                label = classify.classify_clip(imgs)
            except Exception as e:  # noqa: BLE001 — 개별 clip 실패는 격리하고 나머지 진행
                print(f"  [{i}/{len(top)}] {c.id[:8]} 실패: {e}", file=sys.stderr)
                label = {"action": "error", "confidence": 0.0}
            action, conf = label.get("action", "unseen"), label.get("confidence", 0.0)
            results.append((c, action, conf))
            # error(claude 호출 실패=한도/인증)는 기록 안 함 → 재개 때 재시도. 그 외(게코판정·unseen)만 done 처리.
            if args.out and action != "error":
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("a") as f:
                    f.write(json.dumps({
                        "clip_id": c.id, "camera_id": c.camera_id, "r2_key": c.r2_key,
                        "started_at": c.started_at, "duration_sec": c.duration_sec,
                        "motion_score": c.motion_score, "action": action,
                        "confidence": conf, "reasoning": label.get("reasoning", ""),
                    }, ensure_ascii=False) + "\n")
            print(f"  [{i:3d}/{len(top)}] {_kst(c.started_at)}  {action:13s} (conf {conf})  {c.id[:8]}")

    _summary(results)
    if args.out:
        print(f"\n결과 저장: {args.out}")
    return 0


def _summary(results) -> None:
    actions = Counter(a for _, a, _ in results)
    errors = actions.get("error", 0)
    valid = len(results) - errors
    seen = sum(1 for _, a, _ in results if a not in ("unseen", "error"))
    print("\n" + "=" * 50)
    pct = f" ({round(100 * seen / valid)}%)" if valid else ""
    err_note = f"  · 분류실패(한도/인증 등) {errors}" if errors else ""
    print(f"요약: 게코 보임 {seen}/{valid}{pct}{err_note}")
    for act, cnt in actions.most_common():
        print(f"  {act:13s} {cnt}")
    feeding = sum(actions.get(a, 0) for a in _FEEDING)
    signals = []
    if actions.get("shedding"):
        signals.append(f"🧬탈피 {actions['shedding']}")
    if actions.get("drinking"):
        signals.append(f"💧음수 {actions['drinking']}")
    if feeding:
        signals.append(f"🍽급여 {feeding}")
    if signals:
        print("  케어 시그널: " + " · ".join(signals))
    print("=" * 50)


if __name__ == "__main__":
    sys.exit(main())
