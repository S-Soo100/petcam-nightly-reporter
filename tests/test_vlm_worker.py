from datetime import datetime
from zoneinfo import ZoneInfo
from tests._fakes import FakeSB
from reporter.vlm_candidate_worker import run

def test_disabled_worker_does_not_touch_db():
    sb=FakeSB();assert run(sb=sb,now=datetime(2026,7,15,22,tzinfo=ZoneInfo("Asia/Seoul")),enabled=False)==0
    assert sb.store=={}
