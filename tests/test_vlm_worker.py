from datetime import datetime
from zoneinfo import ZoneInfo
from tests._fakes import FakeSB
from reporter.vlm_candidate_worker import cap_night_selections, failure_status, run
from reporter.vlm_models import CandidateClip, SelectedCandidate, Slot

def test_disabled_worker_does_not_touch_db():
    sb=FakeSB();assert run(sb=sb,now=datetime(2026,7,15,22,tzinfo=ZoneInfo("Asia/Seoul")),enabled=False)==0
    assert sb.store=={}


def _selected(camera_id, slot, clip_id):
    clip=CandidateClip(clip_id,camera_id,datetime(2026,7,15,22,tzinfo=ZoneInfo("Asia/Seoul")),30,"k",1,1280,720)
    return SelectedCandidate(clip,slot,clip_id,{},slot.value)


def test_night_cap_is_fair_and_respects_camera_limit():
    selected={
        "A":[_selected("A",s,f"a{i}") for i,s in enumerate(Slot)],
        "B":[_selected("B",s,f"b{i}") for i,s in enumerate(Slot)],
    }
    capped=cap_night_selections(selected,{"A":15,"B":0},3)
    assert [x.slot for x in capped["A"]]==[Slot.CUSTOMER_HIGHLIGHT]
    assert [x.slot for x in capped["B"]]==[Slot.CUSTOMER_HIGHLIGHT,Slot.SUBTLE_BEHAVIOR]


def test_failure_status_allows_exactly_two_attempts():
    assert failure_status({"attempt_count":0})=="failed_retryable"
    assert failure_status({"attempt_count":1})=="failed_terminal"
    assert failure_status({"attempt_count":2})=="failed_terminal"
