"""정규 candidate worker 의 fail-closed host guard (설계 §6.1).

production host 는 검증된 Mac mini 한 대뿐이다. installer 가 현재 host 를 자동 승인하지
않도록, expected host 는 배포자가 명시한 값이어야 하며 미설정/불일치면 DB·Claude·Slack
어느 것도 건드리기 전에 fail-closed 한다.
"""

from __future__ import annotations


def _safe_label(value: str | None) -> str:
    """host 문자열에 secret 은 없지만 newline/control 문자를 로그에 넣지 않게 printable subset 만."""
    if not value:
        return "<unset>"
    cleaned = "".join(ch for ch in value.strip() if ch.isprintable())
    return cleaned[:64] or "<unset>"


class HostOwnershipError(RuntimeError):
    def __init__(self, actual: str | None, expected: str | None):
        self.actual = _safe_label(actual)
        self.expected = _safe_label(expected)
        super().__init__(f"host mismatch actual={self.actual} expected={self.expected}")


def require_expected_host(actual: str | None, expected: str | None) -> None:
    """실행 host 가 expected 와 정확히 일치하지 않으면 HostOwnershipError. short↔FQDN 자동 동치 금지."""
    normalized_expected = (expected or "").strip()
    if not normalized_expected:
        raise HostOwnershipError(actual, expected)
    if (actual or "").strip() != normalized_expected:
        raise HostOwnershipError(actual, expected)
