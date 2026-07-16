# activity-worker Single-Host Migration — 설계

> 목적: `activity-worker` 의 유일한 production runtime 을 잘못 배치된 MacBook 에서 상시 가동
> Mac mini(`baeg-endeuui-Macmini.local`) 로 단일 이전. 근본 원인은 (a) host guard 부재로
> 어느 host 에서나 실행 가능, (b) partial-failure 를 exit 0 으로 숨겨 부분 실패가 정상처럼 기록.

## 1. 근본 원인 (systematic-debugging)

- **오배치**: `com.petcam.activity-worker` 가 MacBook(`BaekBook-Pro-14-M5.local`, working dir
  `/Users/baek/petcam-nightly-reporter`)에 loaded, runs=38. Mac mini 는 absent. VLM candidate
  worker 는 `require_expected_host` fail-closed guard 가 있으나 activity worker 에는 **없음** →
  host 검증 없이 실행.
- **partial-failure 은닉**: `reporter.activity_worker.run()` 이 `process_batch` 의 `failed`
  카운트와 무관하게 항상 `return 0`. MacBook 네트워크 불안 cycle(`queried 55 / ok 43 / fail 12`,
  `RemoteProtocolError`/`ConnectError`/DNS)이 exit 0 으로 기록됨.
- **로그 위생**: clip 실패 로그가 `type(e).__name__: {e}` 로 예외 전문을 출력 → DB/HTTP 예외에
  URL·토큰이 섞일 수 있음.

## 2. 변경 범위 (In)

| 파일 | 변경 |
|---|---|
| `reporter/config.py` | `ACTIVITY_EXPECTED_HOST` env 추가 (기본 `""`) |
| `reporter/activity_worker.py` | (1) `require_expected_host` 재사용한 fail-closed host guard 를 lock/DB/R2/detector/policy guard 보다 먼저. (2) `run()` 이 `stats["failed"]>0` 이면 nonzero 반환. (3) clip-skip 로그에서 예외 전문 제거(타입명만) |
| `install-launchd-activity.sh` | `ACTIVITY_EXPECTED_HOST` 누락/불일치 시 설치 중단, plist 에 expected host 주입, 자동 승인 금지 |
| `tests/test_activity_worker.py` | 기존 run() 테스트에 guard-통과 인자 주입 + host guard/partial-failure/로그위생 RED→GREEN 테스트 추가 |
| `tests/test_install_activity_launchd.py` (신규) | installer 계약 테스트 |

## 3. 범위 밖 (Out) — 변경 금지

Python Evidence Hybrid selector, VLM batch, Gate threshold, DB schema, exclusion 설정, 앱
effective activity 정책, VLM/backfill/finalizer/nightly/router-features LaunchAgent. policy
preset(`activity-v0/v1`) 값과 `build_activity_policy` 로직 불변.

## 4. Host guard 계약 (§5.1)

- `activity_worker.run(*, hostname_fn=socket.gethostname, expected_host=None, ...)`.
- `expected = config.ACTIVITY_EXPECTED_HOST if expected_host is None else expected_host`.
- `require_expected_host(hostname_fn(), expected)` 를 **lock 획득·`create_client`·`load_enabled_cameras`·
  `load_detector`·R2·Slack 이전**에 호출. `HostOwnershipError` 시 secret 없는 라벨만 로그 + nonzero(예: 2) 반환.
- guard 실패 시 detector/download/store/DB 호출 0회 (기존 `vlm_host_guard` 로직 그대로 재사용,
  short↔FQDN 자동 동치 금지, 공백 expected fail-closed).

## 5. Partial-failure 관측성 계약 (§5.3)

- 성공 clip 결과는 그대로 저장(격리 유지). `run()` 최종 반환: `1 if stats["failed"] else 0`.
- early-return 경로(카메라 없음/policy 불일치/미처리 clip 0)는 실패가 아니므로 `0` 유지.
- clip-skip 로그는 `type(e).__name__` 만 — 예외 전문/URL/secret 미출력. summary(`_log`)는 이미
  `queried/ok/fail` 출력.

## 6. Installer 계약 (§5.2)

- `EXPECTED_HOST="${ACTIVITY_EXPECTED_HOST:-}"`, `ACTUAL_HOST="$(hostname)"`.
- `-n EXPECTED_HOST` 아니면 abort, `ACTUAL_HOST != EXPECTED_HOST` 면 abort.
- `ACTIVITY_EXPECTED_HOST="$ACTUAL_HOST"` 형태의 자동 승인 금지(정적 검사).
- plist EnvironmentVariables: `ACTIVITY_EXPECTED_HOST=<expected>` + `ACTIVITY_POLICY_VERSION=activity-v1`.
- `plutil -lint` 실패 시 bootstrap 금지(기존 유지). WorkingDirectory/RunAtLoad/StartInterval=3600 유지.
- VLM/backfill installer 무수정.

## 7. Runtime 이전 (§8~9)

worktree(`origin/main` 기반, `fix/activity-worker-single-host`) 에서 코드+테스트+docs 커밋 →
fast-forward push → handoff manifest + `HANDOFF_OK` → Mac mini preflight(read-only baseline) →
MacBook 비파괴 bootout(백업 보존) → Mac mini render/lint→bootstrap → 전체 loaded 수 정확히 1 →
첫 실사이클 acceptance(`queried=ok`, `fail=0`, exit 0). 실패 시 MacBook 자동 복구 금지, Mac mini
bootout 후 증거와 함께 중단.
