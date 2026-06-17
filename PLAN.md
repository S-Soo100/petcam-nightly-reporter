# petcam-nightly-reporter 계획

작성일: 2026-06-10  
상태: 기획 초안  
상위 맥락: `/Users/baek/petcam-lab`의 RBA 경험을 바탕으로 만든 별도 프로젝트

## 1. 한 줄 정의

`petcam-nightly-reporter`는 여러 카메라가 R2에 업로드한 motion clip을 매일 밤 단위로 모아, event/frame bundle로 압축하고, Codex가 아침 리포트를 작성하는 개인용 야간 관제 리포터다.

최종 목표는 사용자의 Mac이 꺼져 있어도 동작하는 완전 클라우드 자동화다.

핵심 방향:

```text
카메라 motion clip 업로드
→ R2 object 색인
→ event/frame bundle 생성
→ Codex 분석
→ 야간 관제 리포트 작성
```

## 2. 왜 별도 프로젝트인가

`petcam-lab`은 API, 캡처, DB, R2, 앱 연동을 담당하는 백엔드다.

이 프로젝트는 그 위에서 동작하는 batch/reporting worker다.

- 카메라 업로드 산출물을 읽는다.
- 원본 mp4를 직접 의미 분석하지 않고, 분석용 중간표현을 만든다.
- 초기 개발은 Codex CLI를 리포트 작성/판단에 사용한다.
- 완전 클라우드 자동화 단계에서는 서버용 API key 기반 모델 호출도 검토한다.
- production API와 분리해서 실험 속도를 높인다.
- 실패해도 `petcam-lab`의 캡처/조회 기능을 흔들지 않는다.

초기 원칙:

```text
petcam-lab = 영상/메타/앱 백엔드
petcam-nightly-reporter = 야간 batch 분석/리포트
```

## 3. 전제

카메라는 움직임을 감지하면 R2에 mp4를 업로드한다.

카메라는 행동 의미를 모른다.

```text
카메라가 아는 것:
- motion이 있었다
- 몇 시에 녹화했다
- 어느 카메라에서 찍었다

후단이 판단할 것:
- 먹이/물/배변/탈피/이동/이상 징후 후보
- 확인이 필요한 clip
- 밤 전체 활동 패턴
```

## 4. 사용자 체험 시뮬레이션

`[화면]` 사용자는 아침에 `reports/nightly/YYYY-MM-DD.md` 또는 앱/메신저 요약을 본다.  
`[조작]` "확인 필요" 섹션에서 중요한 이벤트만 열어본다.  
`[반응]` 카메라별 밤사이 활동, 물/먹이 후보, 배변/탈피 의심, 이상 징후가 시간순으로 정리된다.  
`[감정]` 사용자는 긴 야간 영상을 뒤지지 않고 "밤사이 무슨 일이 있었는지"를 빠르게 이해한다.

상세 검수:

`[화면]` 이벤트 항목에는 시간, 카메라, 핵심 프레임, 판단 근거, confidence/needs_review가 보인다.  
`[조작]` 사용자가 원본 clip 또는 10~20개 key frame을 확인한다.  
`[반응]` 애매한 건 `needs_review`로 남고, 확실한 건 리포트의 케어 신호에 반영된다.  
`[감정]` AI가 단정하지 않고 근거와 불확실성을 같이 말한다고 느낀다.

## 5. MVP 목표

첫 버전은 "잘 돌아가는 아침 리포트"가 목표다.

단, MVP를 두 단계로 나눈다.

```text
MVP-0: 로컬 개발/검증
MVP-1: 사용자의 Mac이 꺼져도 동작하는 클라우드 자동화
```

MVP-0은 알고리즘과 리포트 품질 검증용이다. `launchd`나 로컬 Codex CLI는 이 단계에서만 쓴다.

MVP-1부터는 스케줄러, 전처리 worker, 리포트 생성이 모두 클라우드에서 돈다.

In:

- R2 prefix에서 특정 밤 범위의 mp4 목록 읽기
- camera id, started_at, duration 색인
- mp4에서 motion/event 후보 추출
- event당 10~20개 개별 프레임 추출
- contact sheet는 보조 요약용으로만 생성
- event metadata JSON 생성
- Codex CLI로 리포트 작성
- `reports/nightly/YYYY-MM-DD.md`와 `.json` 저장

Out:

- 실시간 알림
- Flutter 앱 연동
- Supabase write-back
- Hermes cron 도입
- YOLO/fine-tune/custom detector
- 행동 모델 학습 자동화
- 원본 mp4 전체를 Codex에 직접 넣는 방식

## 6. 비권장 설계

원본 mp4를 모두 Codex/Hermes에 그대로 넣지 않는다.

```text
나쁜 흐름:
R2 mp4 전체
→ Codex/Hermes에 통째 업로드
→ 긴 자유형 요약
```

이 방식은 비용/한도 예측이 어렵고, 짧은 미세 행동을 놓치기 쉽다.

## 7. 권장 설계

```text
R2 mp4
→ 저해상도 motion scan
→ event boundary
→ event별 10~20 frames
→ ROI crop / motion heatmap / metadata
→ Codex report prompt
→ nightly report
```

중요 구분:

- `1~2fps scan`은 이벤트를 찾기 위한 저렴한 탐지 단계다.
- 의미 분석 입력은 event당 `10~20개 개별 프레임`이다.
- contact sheet는 빠른 사람 검토/요약용이지 주 입력이 아니다.
- 원본 mp4는 재검수/증거 보관용으로 남긴다.

## 8. 데이터 흐름

### 8.1 R2 object layout

권장 원본 key:

```text
raw/{camera_id}/{yyyy}/{mm}/{dd}/{hh}/{timestamp}_{clip_id}.mp4
raw/{camera_id}/{yyyy}/{mm}/{dd}/{hh}/{timestamp}_{clip_id}.json
```

sidecar JSON은 있으면 좋고, 없으면 파일명과 `ffprobe`로 보완한다.

sidecar 예시:

```json
{
  "camera_id": "cam-main",
  "started_at": "2026-06-10T02:14:05+09:00",
  "ended_at": "2026-06-10T02:14:48+09:00",
  "trigger": "motion",
  "firmware_motion_score": 0.74
}
```

### 8.2 derived artifact layout

```text
artifacts/
  derived/
    {yyyy-mm-dd}/
      {camera_id}/
        {clip_id}/
          manifest.json
          events.jsonl
          frames/
            e00_f00_02.10s.jpg
            e00_f01_02.45s.jpg
          crops/
            e00_water_bowl_f00.jpg
          heatmaps/
            e00_motion_heatmap.png
          contact/
            e00_contact.jpg

reports/
  nightly/
    2026-06-10.md
    2026-06-10.json
```

## 9. Event bundle 계약

`events.jsonl` 한 줄 예시:

```json
{
  "event_id": "cam-main:2026-06-10T02:14:05:e00",
  "camera_id": "cam-main",
  "clip_id": "clip123",
  "start_sec": 8.5,
  "end_sec": 25.0,
  "duration_sec": 16.5,
  "peak_changed_ratio": 1.08,
  "mean_changed_ratio": 0.42,
  "motion_centroid": [0.62, 0.44],
  "nearest_roi": "water_bowl",
  "key_frames": [
    "frames/e00_f00_08.50s.jpg",
    "frames/e00_f01_09.20s.jpg"
  ],
  "contact_sheet": "contact/e00_contact.jpg",
  "motion_heatmap": "heatmaps/e00_motion_heatmap.png",
  "needs_review_hint": true
}
```

## 10. 프레임 샘플링 정책

1프레임 대표 이미지는 행동 분석용으로 쓰지 않는다.

기본 정책:

| event 유형 | 프레임 수 | 이유 |
|---|---:|---|
| 단순 이동 후보 | 5~8 | 비용 절감 |
| 일반 event | 10~16 | 기본 분석 |
| P0 후보 | 20 | 먹이/물/배변/탈피 회복률 우선 |
| 불확실/확인 필요 | 20 | HITL 검수 근거 확보 |

샘플링 방식:

- event 전체 균등 샘플
- motion peak 주변 추가 샘플
- start/end context 포함
- perceptual hash 또는 SSIM으로 중복 프레임 제거
- contact sheet는 개별 프레임을 대체하지 않는다

## 11. 기술 스택 초안

언어/런타임:

- Python 3.12
- uv

영상 처리:

- OpenCV: frame scan, absdiff, crop, heatmap
- FFmpeg/ffprobe: duration, codec, fallback frame extraction

스토리지:

- Cloudflare R2 S3-compatible API
- local artifacts cache
- 초기 상태 저장은 SQLite

리포트/AI:

- Codex CLI MVP-0
- MVP-1부터는 OpenAI/Gemini/Claude API key 기반 호출 검토
- Hermes는 후순위

스케줄:

- 로컬 Mac: `launchd` (MVP-0 개발/검증 전용)
- 서버: `systemd timer`
- Python 내부 반복이 필요하면 APScheduler
- 완전 클라우드: Cloudflare Cron Triggers 또는 Fly.io scheduled job
- R2 event 기반 ingestion: Cloudflare R2 Event Notifications + Queues

클라우드 compute 후보:

- Fly.io/Railway/Render: Python + OpenCV + FFmpeg 컨테이너를 쉽게 운영.
- Cloudflare Containers: R2/Queues/Cron과 가장 잘 붙는 장기 구조.
- Cloudflare Workers 단독: 인덱싱/큐잉/가벼운 orchestration에는 좋지만, FFmpeg/OpenCV 같은 무거운 영상 처리에는 부적합.

## 12. Codex vs Hermes vs 서버 API 결정

MVP-0은 Codex 단독으로 간다.

```text
Python worker
→ manifest/frame bundle 생성
→ codex exec로 report 생성
```

Hermes를 지금 넣지 않는 이유:

- Hermes가 Codex의 분석 품질을 올리지는 않는다.
- 스케줄링은 launchd/systemd/cron으로 충분하다.
- 초기 디버깅은 Python + Codex CLI가 단순하다.

하지만 완전 클라우드 자동화에서는 Codex CLI가 최종 답이 아닐 수 있다.

이유:

- Codex CLI/OAuth는 개인 개발 환경에는 편하지만, 무인 클라우드 batch 인증/갱신/한도 관리가 애매할 수 있다.
- 서버에서는 API key 기반 OpenAI/Gemini/Claude 호출이 운영상 더 단순하다.
- 리포트 생성은 coding agent 능력보다 structured vision/text analysis와 안정적 batch 실행이 중요하다.

MVP-1의 기본 판단:

```text
클라우드 전처리 worker = Python/OpenCV/FFmpeg
클라우드 리포트 생성 = API key 기반 모델 호출 우선 검토
Codex CLI = 로컬 개발/분석 보조
Hermes = 후순위 automation shell
```

Hermes 도입 기준:

- Telegram/Slack/Discord/email delivery가 필요하다.
- Codex 실패 시 Claude/Gemini/OpenRouter fallback이 필요하다.
- 자연어로 cron을 관리하고 싶다.
- 장기 memory/skill 기반 운영 자동화가 필요하다.
- 여러 agent를 병렬로 돌려야 한다.

## 13. 리포트 형식

Markdown 리포트 예시:

```markdown
# 2026-06-10 야간 리포트

관제 범위: 2026-06-09 19:00 ~ 2026-06-10 06:00

## 요약
- 카메라: 3대
- motion clip: 184개
- 의미 있는 event: 17개
- 확인 필요: 4개

## 주요 이벤트
- 02:14 cam-main: 물그릇 근처 체류 21초, 수분 행동 후보
- 03:32 cam-side: 먹이그릇 접근 후 반복 머리 움직임, 섭식 후보
- 05:48 cam-main: 배변 의심 자세, 확인 필요

## 케어 신호
- 활동량은 평소보다 높음
- 수분 행동 후보 2회
- 섭식 후보 1회
- 탈피/상처 의심 없음

## 확인 필요
- cam-main 05:48 event_03: 배변 의심, 프레임 확인 권장
```

JSON 리포트는 앱/후속 자동화를 위해 함께 저장한다.

## 14. 실행 흐름

### 14.1 MVP-0: 로컬 개발/검증 흐름

```text
06:00 nightly window close
06:03 build bundle
06:05 run Codex report
06:10 save reports
```

명령 초안:

```bash
uv run python -m reporter.build_bundle \
  --date 2026-06-10 \
  --from 2026-06-09T19:00:00+09:00 \
  --to 2026-06-10T06:00:00+09:00

codex exec "reports/input/2026-06-10/manifest.json을 읽고 reports/nightly/2026-06-10.md와 .json 리포트를 작성해"
```

이 흐름은 사용자의 Mac이 켜져 있을 때만 동작한다. 제품 목표가 아니라 개발용 검증 루프다.

### 14.2 MVP-1: 완전 클라우드 자동화 흐름

```text
카메라
→ R2 raw/{camera_id}/...mp4 업로드
→ R2 Event Notification
→ Cloudflare Queue
→ cloud worker/container가 clip 인덱싱
→ Python/OpenCV/FFmpeg worker가 derived bundle 생성
→ 06:05 cloud scheduler가 nightly report job 실행
→ report를 R2/Supabase/Slack/Email 중 선택한 대상에 저장/전송
```

이 단계부터 사용자의 Mac이 꺼져 있어도 동작한다.

### 14.3 MVP-1 추천 배포안: Fly.io worker + R2

초기 클라우드 자동화는 Fly.io 같은 일반 Linux 컨테이너가 가장 쉽다.

```text
R2 raw mp4
→ Fly.io Python worker
→ OpenCV/FFmpeg 전처리
→ R2 derived artifacts 저장
→ Fly scheduled job 또는 외부 cron trigger
→ report 생성
```

장점:

- Python/OpenCV/FFmpeg 운영이 쉽다.
- 현재 `petcam-lab`의 worker 운영 감각과 비슷하다.
- 영상 처리 디버깅이 Cloudflare Workers/Containers보다 단순하다.

단점:

- R2 Event Notification과 직접 붙이는 데 Cloudflare 단독 구조보다 한 단계가 더 있다.
- 장기적으로 Cloudflare-first 구조보다 서비스가 하나 더 늘어난다.

### 14.4 장기 배포안: Cloudflare-first

장기적으로는 R2 주변에서 모든 이벤트와 스케줄을 처리하는 구조가 깔끔하다.

```text
R2 Event Notifications
→ Cloudflare Queues
→ Worker consumer
→ Cloudflare Container에서 영상 전처리
→ R2 derived artifacts
→ Workers Cron Trigger가 nightly report job 실행
```

장점:

- R2, Queue, Cron, Container가 같은 플랫폼 안에 있다.
- object upload event 기반 파이프라인이 자연스럽다.
- 사용자의 로컬 머신과 완전히 분리된다.

단점:

- Cloudflare Containers 운영 학습이 필요하다.
- OpenCV/FFmpeg batch 디버깅은 일반 VPS/Fly보다 낯설 수 있다.

## 15. 초기 디렉토리 구조 목표

```text
petcam-nightly-reporter/
  PLAN.md
  README.md
  pyproject.toml
  src/
    reporter/
      __init__.py
      config.py
      r2_indexer.py
      video_probe.py
      motion_scan.py
      event_segmenter.py
      frame_sampler.py
      bundle_writer.py
      codex_reporter.py
      report_schema.py
  scripts/
    run_nightly.sh
  artifacts/
    .gitkeep
  reports/
    nightly/
      .gitkeep
  specs/
    .gitkeep
  tests/
```

## 16. 보관 정책

초기 권장:

- raw mp4: 7~30일
- derived frames/json: 30~90일
- nightly reports: 장기 보관

R2 lifecycle rule은 prefix별로 나눠 설정한다.

```text
raw/      짧게 보관
derived/  중간 보관
reports/  길게 보관
```

## 17. 실패/리스크

| 리스크 | 대응 |
|---|---|
| mp4 업로드 중 indexer가 먼저 읽음 | object size 안정화 확인 또는 sidecar 완료 파일 사용 |
| 카메라 시간이 틀림 | server-side object timestamp와 sidecar 비교 |
| 중복 업로드 | object key + etag + duration으로 dedupe |
| 너무 많은 clip | event cap, camera별 budget, moving-only 축약 |
| Codex 한도 초과 | frame 수 줄이기, 카메라별 chunk report 후 final merge |
| 미세행동 누락 | P0 후보는 20 frames + ROI crop |
| AI 과단정 | `candidate / inferred / confirmed / needs_review` 레벨 사용 |
| 사용자의 Mac이 꺼짐 | MVP-1부터 cloud scheduler/worker로 이전 |
| Codex OAuth 무인 실행 불안정 | 서버용 API key 기반 모델 호출로 전환 |
| Cloudflare Worker에서 영상 처리 한계 | 무거운 처리는 Fly.io 또는 Cloudflare Container로 분리 |

## 18. 다음 구현 순서

### 18.1 MVP-0: 로컬 검증

1. `pyproject.toml`과 기본 패키지 구조 생성
2. `.env.example` 작성
3. R2 list/indexer 구현
4. local mp4 대상 `motion_scan.py` 구현
5. `event_segmenter.py` 구현
6. event당 10~20 frames 추출
7. bundle manifest 스키마 확정
8. Codex report prompt 작성
9. 샘플 1박치 mock/local run
10. 수동 nightly command로 리포트 품질 검증

### 18.2 MVP-1: 완전 클라우드 자동화

1. Dockerfile 작성: Python 3.12 + uv + OpenCV + FFmpeg
2. R2 credentials와 bucket/prefix 설정 분리
3. cloud worker가 R2에서 nightly 대상 clip을 읽도록 구현
4. derived artifacts를 R2에 다시 쓰기
5. SQLite 대신 cloud-safe 상태 저장 선택
   - 초기: R2 manifest files
   - 운영: Supabase 또는 D1/Postgres
6. cloud scheduler 연결
   - 쉬운 시작: Fly.io scheduled job
   - 장기 구조: Cloudflare Cron Trigger
7. API key 기반 report model 호출 구현
8. report를 R2 `reports/` prefix에 저장
9. 실패 로그와 retry 정책 추가
10. 알림 채널은 report 안정화 후 추가

## 19. 미결정 질문

- 카메라가 sidecar JSON을 같이 올릴 수 있나?
- R2 key naming을 제어할 수 있나?
- camera별 ROI 설정을 어디에 둘까? local YAML, Supabase, R2 config 중 선택 필요.
- 리포트 전달 채널은 파일만으로 시작할까, Telegram/Slack까지 바로 갈까?
- raw mp4 retention은 며칠이 적당한가?
- Codex 입력은 카메라별 chunk 후 final merge가 좋을까, 전체 manifest 1회 호출이 좋을까?
- 완전 클라우드 report model은 무엇으로 갈까?
  - OpenAI API
  - Gemini API
  - Claude API
  - Codex CLI/OAuth 유지
- MVP-1 배포는 Fly.io부터 갈까, Cloudflare Containers부터 갈까?
- R2 Event Notifications를 바로 쓸까, 처음엔 매일 아침 R2 list batch로 단순화할까?

## 20. 현재 결론

현재 결론은 두 단계다.

로컬 검증:

```text
R2에 쌓인 motion mp4
→ Python/OpenCV로 10~20 frame bundle
→ Codex CLI가 매일 아침 리포트 작성
→ Hermes는 MVP 이후 자동화 shell로 검토
```

완전 클라우드 자동화:

```text
R2 motion mp4
→ cloud Python/OpenCV/FFmpeg worker
→ R2 derived frame bundle
→ cloud scheduler
→ API key 기반 모델이 nightly report 작성
→ report를 R2/Supabase/메신저에 저장/전송
```

개발은 Codex CLI로 빠르게 시작하되, 사용자의 컴퓨터가 꺼져도 동작하는 목표는 Fly.io 또는 Cloudflare 기반 cloud worker로 달성한다.
