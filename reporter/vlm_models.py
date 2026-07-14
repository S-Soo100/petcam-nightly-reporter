from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Slot(str, Enum):
    CUSTOMER_HIGHLIGHT="customer_highlight"; SUBTLE_BEHAVIOR="subtle_behavior"
    DIVERSITY_DISCOVERY="diversity_discovery"; EXCLUSION_AUDIT="exclusion_audit"


@dataclass(frozen=True, slots=True)
class CandidateClip:
    id:str; camera_id:str; started_at:datetime; duration_sec:float; r2_key:str|None
    motion_score:float; width:int|None; height:int|None
    assessment_id:str|None=None; activity_decision:str|None=None; prelabel_id:str|None=None
    gecko_visible:bool|None=None; visibility_confidence:float|None=None
    gecko_bbox:tuple[float,float,float,float]|None=None
    motion_metrics:dict[str,object]=field(default_factory=dict); hard_skip_reason:str|None=None


@dataclass(frozen=True, slots=True)
class EpisodeRepresentative:
    clip:CandidateClip; episode_key:str; motion_quartile:int
    bbox_bucket:tuple[int|str,int|str,str]


@dataclass(frozen=True, slots=True)
class SelectedCandidate:
    clip:CandidateClip; slot:Slot; episode_key:str
    rank_features:dict[str,object]; selection_reason:str
