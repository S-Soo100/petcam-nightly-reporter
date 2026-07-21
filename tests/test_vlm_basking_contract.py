from pathlib import Path

from reporter import anthropic_analyzer, classify, config
from reporter import claude_cli_analyzer as cli


def test_basking_is_canonical_in_every_vlm_schema():
    assert "basking" in cli._SCHEMA["properties"]["items"]["items"]["properties"]["action"]["enum"]
    assert "basking" in anthropic_analyzer.OUTPUT_SCHEMA["properties"]["action"]["enum"]


def test_all_analyzers_use_v41_prompt_and_provenance():
    assert cli._SYSTEM_FILE.name == "system.v4.1.md"
    assert classify._SYSTEM_FILE.name == "system.v4.1.md"
    assert "system.v4.1.md" in str(anthropic_analyzer.SYSTEM_PROMPT_PATH)
    assert config.VLM_PROMPT_VERSION == "v4.1-direct-images"


def test_prompt_defines_basking_moving_unseen_boundary():
    prompt = Path(cli._SYSTEM_FILE).read_text()
    required = (
        "basking",
        "head/eye/gaze",
        "body position",
        "Do NOT infer `moving` merely because the motion-triggered camera recorded the clip",
        "partially occluded",
    )
    assert all(text in prompt for text in required)
