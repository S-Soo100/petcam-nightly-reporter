from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True,slots=True)
class Usage: input_tokens:int; cache_creation_input_tokens:int; cache_read_input_tokens:int; output_tokens:int

def calculate_cost(u):
    v=(Decimal(u.input_tokens)*2+Decimal(u.cache_creation_input_tokens)*Decimal("2.5")+Decimal(u.cache_read_input_tokens)*Decimal("0.2")+Decimal(u.output_tokens)*10)/Decimal(1_000_000)
    return v.quantize(Decimal("0.000001"))

_SLOTS={"customer_highlight":0,"subtle_behavior":1,"diversity_discovery":2,"exclusion_audit":3}
def fair_job_order(jobs):return sorted(jobs,key=lambda j:(_SLOTS[j["slot"]],j["camera_id"],j.get("queued_at","")))

def fair_selection_cap(by_camera,remaining):
    out={k:[] for k in by_camera}
    for slot in _SLOTS:
        for cam in sorted(by_camera):
            for item in by_camera[cam]:
                if item.slot.value==slot and remaining>0:out[cam].append(item);remaining-=1
    return out
