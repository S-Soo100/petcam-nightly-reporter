"""프레임 → claude -p (sonnet) → 행동 라벨 JSON. Slack 전송 주체는 worker(분리 진단).

방식(W3 스파이크로 확정):
- claude 가 Read 도구로 로컬 프레임을 열어서 봄. --add-dir 로 프레임 디렉토리 접근 허용,
  --allowedTools Read 로 Read pre-approve(-p 비대화 모드라 권한 프롬프트에서 멈추는 것 방지).
- --append-system-prompt-file 로 v4.0 프롬프트 주입 (shell escaping 회피, prompts/system.v4.0.md).
- --model sonnet: v4.0 기준선(85.9%)이 Sonnet 4.6 라 모델 정합 (메모리 prompt-model-specificity —
  프롬프트는 모델과 쌍으로만 의미). 기본값 Opus 로 돌면 기준선과 어긋남 + 더 비쌈.
- --output-format json: 결과가 envelope, .result 필드에 모델 텍스트(마크다운 서사 + json 블록).
"""
import json
import subprocess
from pathlib import Path

_SYSTEM_FILE = Path(__file__).parent / "prompts" / "system.v4.0.md"


def classify_clip(frame_paths: list[Path]) -> dict:
    """프레임들을 claude(sonnet)에 주고 {action, confidence, reasoning} 반환.

    frame_paths 가 몽타주 1장이어도 동작(W4 비용 대응 시 그리드 1장 전달 가능).
    실패 시 action=error(호출 실패) 또는 unseen(파싱 실패) — 워커가 격리 처리.
    """
    if not frame_paths:
        return {"action": "unseen", "confidence": 0.0}
    frame_dir = str(frame_paths[0].parent)
    listed = " ".join(str(p) for p in frame_paths)
    user = (
        f"다음은 게코 사육장 CCTV 프레임 {len(frame_paths)}장(시간순)이야. "
        f"각 파일을 Read 도구로 열어서 보고, 게코의 대표 행동 1개를 v4.0 7-class 로 분류해. "
        f'JSON {{"action":..., "confidence":0~1, "reasoning":...}} 만 출력. 프레임: {listed}'
    )
    try:
        r = subprocess.run(
            ["claude", "-p", user,
             "--append-system-prompt-file", str(_SYSTEM_FILE),
             "--allowedTools", "Read",
             "--add-dir", frame_dir,
             "--model", "sonnet",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"[classify] FAIL: {e}")
        return {"action": "error", "confidence": 0.0}
    if r.returncode != 0:
        print(f"[classify] rc={r.returncode}: {r.stderr.strip()[:200]}")
        return {"action": "error", "confidence": 0.0}
    # claude 는 인증·한도 실패도 rc=0 + envelope {"is_error": true, "result": "Not logged in …"}
    # 형태로 반환한다. rc 만 검사하면 이 에러가 _parse 에서 조용히 unseen 으로 새서(로그 0바이트로
    # 남지도 않아) "행동만 빈 리포트"가 며칠 지속돼도 못 알아챈다(2026-07-07 실측). envelope 레벨에서 먼저 잡는다.
    try:
        envelope = json.loads(r.stdout)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict) and envelope.get("is_error"):
        print(f"[classify] api_error: {str(envelope.get('result'))[:200]}")
        return {"action": "error", "confidence": 0.0}
    return _parse(r.stdout)


def _parse(stdout: str) -> dict:
    """claude --output-format json 은 envelope. .result 텍스트에서 행동 JSON 블록 추출.

    result 는 '관찰 요약'(마크다운) + ```json {...}``` 형태라 첫 '{' ~ 마지막 '}' 슬라이스로
    json 블록만 잡는다(서사엔 중괄호 없음 · reasoning 문자열엔 '}' 안 씀).
    """
    try:
        envelope = json.loads(stdout)
        text = envelope.get("result", stdout) if isinstance(envelope, dict) else stdout
    except json.JSONDecodeError:
        text = stdout
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"action": "unseen", "confidence": 0.0}  # 파싱 실패 = 보수적 unseen
