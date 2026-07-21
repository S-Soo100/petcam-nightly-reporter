import base64,hashlib,json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from reporter.vlm_budget import Usage,calculate_cost

SYSTEM_PROMPT=(Path(__file__).parent/"prompts/system.v4.0.md").read_text()
OUTPUT_SCHEMA={"type":"object","properties":{"action":{"type":"string","enum":["eating_paste","eating_prey","drinking","shedding","moving","unseen","hand_feeding"]},"confidence":{"type":"number","minimum":0,"maximum":1},"reasoning":{"type":"string","maxLength":300}},"required":["action","confidence","reasoning"],"additionalProperties":False}
@dataclass(frozen=True,slots=True)
class AnalyzerResult:
    provider_request_id:str;model_requested:str;model_actual:str;result:dict;usage:Usage;cost_usd:Decimal;model_mismatch:bool
def _image(path):return {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":base64.b64encode(path.read_bytes()).decode()}}
def analyze_clip(client,paths,clip,model):
    if model=="sonnet" or not model.startswith("claude-"):raise ValueError("exact model id required")
    content=[_image(p) for p in paths]+[{"type":"text","text":f"duration={clip.duration_sec:.3f}s. Frames are chronological. Classify one representative behavior."}]
    m=client.messages.create(model=model,max_tokens=256,temperature=0,system=[{"type":"text","text":SYSTEM_PROMPT,"cache_control":{"type":"ephemeral"}}],messages=[{"role":"user","content":content}],output_config={"format":{"type":"json_schema","schema":OUTPUT_SCHEMA}},metadata={"user_id":hashlib.sha256(clip.id.encode()).hexdigest()})
    block=next((b for b in m.content if b.type=="text"),None)
    if block is None:raise ValueError("provider response missing text")
    result=json.loads(block.text);u=Usage(m.usage.input_tokens,getattr(m.usage,"cache_creation_input_tokens",0) or 0,getattr(m.usage,"cache_read_input_tokens",0) or 0,m.usage.output_tokens)
    return AnalyzerResult(m.id,model,m.model,result,u,calculate_cost(u),m.model!=model)
