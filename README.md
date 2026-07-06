# petcam-nightly-reporter

> 맥미니가 야간에 깨어나 terra `motion_clips`의 최근 2h 윈도우를 분석 → **활동량·활동시간대 + 행동 샘플**을 Slack 리포트로 보내는 상시 워커. RBA 파이프라인의 "리포팅 공장".

- **구현 계획**: `specs/plan-window-worker-mvp0.md` (W0~W5)
- **상위 설계**: `specs/architecture.md`
- **참조 골격**: `petcam-mac-runner` (launchd/스모크 패턴 이식원)
- **토폴로지**: `tera-ai-product-master/docs/specs/petcam-ai-pipeline.md §11`

## 무엇을 하나

윈도우(2h)마다: `motion_clips` 조회 → **활동량·시간대 집계(DB, claude 0회)** + **`motion_score` 상위 N개만 claude 행동 태깅**(탈피·음수·급여) → Slack 카드 1장. 돌고 죽는다(launchd 재실행).

리포트 뼈대 = 활동량 + 활동시간대 + 행동시그널. 미세행동 정밀 카운트·배변은 스코프 밖.

## 아키텍처 (비용 설계)

W3 실측: clip당 **~12만 토큰**. 전량 밤배치 = 구독 한도 초과. 그래서 두 층으로 분리:
- **뼈대(활동량/시간대)** — `motion_clips` DB 만으로 산출. clip 존재 = 그 시각 활동(모션 트리거). **claude 0회.**
- **행동(탈피/음수/급여)** — `motion_score` 상위 N개(`SAMPLE_TOP_N`)만 claude. 샘플이라 "관찰됨" 수준(부재 증명 아님).
- **한도 전략** — launchd 야간 22/00/02/04시 분산 → 각 배치 직전 2h만 + 5h rolling 부분회복 + 새벽 미사용 독점.

## 셋업 (맥미니)

전제: `claude` CLI 로그인됨 · `uv` 설치됨 · `ffmpeg`/`ffprobe` 설치됨. 경로 확인:
```bash
command -v uv claude ffmpeg    # 셋 다 나와야 함 (launchd PATH 에 들어감)
```

```bash
cd ~
git clone <this-repo-url> petcam-nightly-reporter   # 이미 있으면 git pull
cd petcam-nightly-reporter
uv sync

cp .env.example .env
# .env 편집 (맥북 값 복사): SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / SLACK_WEBHOOK_URL / R2_*
```
⚠️ 비밀값(`.env`)은 git/채팅 금지.

### 수동 검증 (launchd 등록 전)

```bash
# 1) 뼈대만 — claude 0회. 활동량/시간대 + 인프라(Supabase·R2·Slack) 생사 확인
SAMPLE_TOP_N=0 uv run python -m reporter.worker

# 2) 행동 태깅 포함 — claude 2회로 다운→프레임→분류 파이프 확인
SAMPLE_TOP_N=2 uv run python -m reporter.worker
```
→ Slack 채널에 활동 요약 카드. (2)에서 claude 분류까지 오면 통과. `claude -p "say hi"` 로 로그인 상태 먼저 확인(설치≠로그인).

### launchd 등록 (야간 상시)

⚠️ **cron 안 씀.** cron 은 GUI 세션 밖 → claude keychain 접근 불가 → `Not logged in`. LaunchAgent 는 GUI 세션 실행 → OK (mac-runner 2026-06-20 검증). 전제 = **자동로그인 + GUI 세션 상시 + 맥미니 타임존 KST**(스케줄이 로컬 시각).

```bash
chmod +x install-launchd.sh   # 최초 1회
./install-launchd.sh          # 이 머신 $HOME·bin 경로로 plist 생성 + bootstrap
tail -f /tmp/nightly-reporter.log
```
→ 즉시 1회(RunAtLoad) + 야간 22/00/02/04시(KST). ⚠️ 설치 즉시 실행은 `SAMPLE_TOP_N`회 claude 를 부르니, 뼈대만 보려면 위 수동검증(1)을 먼저.

상태/해제:
```bash
launchctl print gui/$(id -u)/com.petcam.nightly-reporter
launchctl bootout gui/$(id -u)/com.petcam.nightly-reporter
rm ~/Library/LaunchAgents/com.petcam.nightly-reporter.plist
```

## 1박 실측 → 튜닝

⚠️ **claude 구독 한도 공유**(메모리 `claude-subscription-quota-shared`): 워커가 본인 claude 작업과 같은 한도를 씀. 1박 돌려 실제 소모를 측정한 뒤:
- `SAMPLE_TOP_N` 조정 (기본 5 → 밤 clip 수·한도 보고)
- 야간 스케줄 조정 (22/00/02/04)
- 정 안되면 전용 계정 / 종량제 API key

## PATH 함정 (install-launchd.sh)

launchd 는 `.zshrc` PATH 를 안 물려받음 → `uv`·`claude`·`ffmpeg` 3개 bin 디렉토리를 plist `PATH` 에 명시(스크립트가 `command -v` 로 실측·중복제거). 맥미니 실측 함정: 세 bin 이 서로 다른 디렉토리일 수 있음(예: `uv`=`~/.local/bin`, `claude`=`/opt/homebrew/bin`).
