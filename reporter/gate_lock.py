"""공통 Gate detector 락 — activity worker 와 detector(RF-DETR) 동시 실행을 막는다.

python_evidence_worker 와 activity_worker 는 같은 Mac mini 에서 각자 detector 를 로드한다. 둘이
동시에 돌면 모델 2개가 메모리/연산을 다투므로, **같은 OS 파일락(`activity_worker._LOCK_PATH` 과 동일
경로)** 을 non-blocking 으로 잡아 한 번에 하나만 detector 를 쓰게 한다. 락 loser 는 실패가 아니라
clean no-op(다음 tick 에 재시도) — job 을 failed 로 만들지 않는다(설계 §11, plan Task 5 Step 3).

activity_worker 를 리팩터하지 않고 같은 경로만 공유하므로 기존 동작은 불변이다. 경로 drift 는
test_gate_lock.py 가 activity_worker._LOCK_PATH 와 동치 검증으로 잡는다.
"""

from __future__ import annotations

import fcntl
import time

# activity_worker._LOCK_PATH 와 반드시 동일해야 상호 배제가 성립한다(drift 테스트로 고정).
COMMON_GATE_LOCK_PATH = "/tmp/petcam-activity-worker.lock"


def acquire_common_gate_lock(path: str = COMMON_GATE_LOCK_PATH):
    """공통 Gate 락을 non-blocking 으로 획득. 이미 다른 프로세스가 쥐고 있으면 None(clean no-op)."""
    fd = open(path, "w")  # noqa: SIM115 — 락은 프로세스 수명 동안 열려 있어야
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


def wait_for_common_gate_lock(
    *, timeout_sec: float, poll_interval_sec: float = 0.1,
    path: str = COMMON_GATE_LOCK_PATH,
):
    """현재 batch 종료까지 bounded wait해서 공개 추론이 다음 batch보다 먼저 락을 잡게 한다."""
    if timeout_sec < 0 or poll_interval_sec <= 0:
        raise ValueError("lock wait timing must be positive")
    deadline = time.monotonic() + timeout_sec
    while True:
        fd = acquire_common_gate_lock(path)
        if fd is not None:
            return fd
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval_sec, remaining))


def release_common_gate_lock(fd) -> None:
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()
