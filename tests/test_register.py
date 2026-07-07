"""register_highlight — camera_clips 미러 + behavior_logs 등록 로직(순수, fake sb).

실 DB 없이 삽입 row 모양·멱등·skip 판정을 고정(donts/python#13 — 네트워크 의존 X).
'조용한 한도실패'처럼, 앱 연동의 회귀는 여기서 막는다.
"""
from reporter import config, register
from reporter.indexer import ClipMeta


class _Result:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """supabase-py 체이닝(.select().eq().insert().execute()) 최소 모사."""

    def __init__(self, table: str, store: dict):
        self._t = table
        self._store = store
        self._filter_id = None

    def select(self, *_):
        return self

    def eq(self, col, val):
        if col == "id":
            self._filter_id = val
        return self

    def insert(self, row):
        self._store.setdefault(self._t, []).append(row)
        return self

    def execute(self):
        if self._filter_id is not None:
            rows = [r for r in self._store.get(self._t, []) if r.get("id") == self._filter_id]
            return _Result(rows)
        return _Result([])


class _FakeSB:
    def __init__(self, preexisting=None):
        self.store = {"camera_clips": list(preexisting or [])}

    def table(self, name):
        return _FakeQuery(name, self.store)


def _clip(cid="c1"):
    return ClipMeta(id=cid, camera_id="cam", started_at="2026-07-07T13:00:00+00:00",
                    duration_sec=42.0, r2_key="clips/x.mp4", motion_score=0.9)


def test_register_inserts_camera_clip_and_behavior_log():
    sb = _FakeSB()
    status = register.register_highlight(sb, _clip(), "drinking", 0.82, "머리 반복 핥기")
    assert status == "registered"
    cc, bl = sb.store["camera_clips"], sb.store["behavior_logs"]
    assert len(cc) == 1 and len(bl) == 1
    # camera_clips 미러: id 재사용 + owner/source/file_path 필수값
    assert cc[0]["id"] == "c1"
    assert cc[0]["user_id"] == config.REGISTER_OWNER_USER_ID
    assert cc[0]["source"] == "camera"
    assert cc[0]["file_path"] == "clips/x.mp4"
    assert cc[0]["has_motion"] is True
    # behavior_logs: vlm 후보(미확정) + 모델 태그
    assert bl[0]["clip_id"] == "c1" and bl[0]["frame_idx"] == 0
    assert bl[0]["action"] == "drinking" and bl[0]["confidence"] == 0.82
    assert bl[0]["source"] == "vlm" and bl[0]["verified"] is False
    assert bl[0]["vlm_model"] == config.REGISTER_VLM_MODEL


def test_register_idempotent_skips_existing():
    # 이미 camera_clips 에 있으면 아무것도 안 쓴다(멱등)
    sb = _FakeSB(preexisting=[{"id": "c1"}])
    status = register.register_highlight(sb, _clip("c1"), "drinking", 0.8)
    assert status == "exists"
    assert "behavior_logs" not in sb.store  # 재삽입 없음


def test_should_register_filters_noninformative():
    assert register.should_register("drinking") is True
    assert register.should_register("shedding") is True
    assert register.should_register("eating_paste") is True
    assert register.should_register("moving") is False   # 흔함
    assert register.should_register("error") is False     # 분류실패
    assert register.should_register("unseen") is False    # 게코 부재
    assert register.should_register("") is False          # 빈 라벨 방어
