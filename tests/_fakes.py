"""테스트용 fake supabase client — supabase-py 체이닝 최소 모사 (실 DB 무의존).

지원: .table().select().eq()/.gte()/.lt()/.in_()/.order()/.limit().execute().data
      .table().insert(row).execute() / .table().upsert(row, on_conflict=...).execute()
donts/python#13: 네트워크/DB 의존 테스트 금지 — 삽입 row 모양·멱등·필터를 여기서 고정.
"""

from __future__ import annotations


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table: str, store: dict):
        self._t = table
        self._store = store
        self._filters: list[tuple] = []
        self._order = None
        self._limit = None
        self._range = None
        self._pending = None  # insert/upsert 가 반환할 row

    # --- read chain ---
    def select(self, *_cols):
        return self

    def eq(self, col, val):
        self._filters.append((col, "eq", val))
        return self

    def gte(self, col, val):
        self._filters.append((col, "gte", val))
        return self

    def gt(self, col, val):
        self._filters.append((col, "gt", val))
        return self

    def lt(self, col, val):
        self._filters.append((col, "lt", val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, "in", list(vals)))
        return self

    def order(self, col, **_kw):
        self._order = col
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (int(start), int(end))  # supabase range 는 [start, end] 양끝 포함
        return self

    # --- write chain ---
    def insert(self, row):
        rows = row if isinstance(row, list) else [row]
        stored = self._store.setdefault(self._t, [])
        out = []
        for r in rows:
            r = dict(r)
            r.setdefault("id", f"{self._t}-{len(stored)}")
            stored.append(r)
            out.append(r)
        self._pending = out
        return self

    def upsert(self, row, on_conflict=None, **_kw):
        keys = on_conflict.split(",") if on_conflict else ["id"]
        stored = self._store.setdefault(self._t, [])
        rows = row if isinstance(row, list) else [row]
        out = []
        for r in rows:
            hit = next((e for e in stored if all(e.get(k) == r.get(k) for k in keys)), None)
            if hit is not None:
                hit.update(r)  # merge (멱등 재실행)
                out.append(hit)
            else:
                r = dict(r)
                r.setdefault("id", f"{self._t}-{len(stored)}")
                stored.append(r)
                out.append(r)
        self._pending = out
        return self

    def update(self, row):
        self._pending = ("update", dict(row))
        return self

    # --- terminal ---
    def execute(self):
        if isinstance(self._pending, tuple) and self._pending[0] == "update":
            rows = [r for r in self._store.get(self._t, []) if self._match(r)]
            for r in rows: r.update(self._pending[1])
            return _Result(rows)
        if self._pending is not None:
            return _Result(self._pending)
        rows = [r for r in self._store.get(self._t, []) if self._match(r)]
        if self._order:
            rows = sorted(rows, key=lambda r: r.get(self._order))
        if self._range is not None:
            lo, hi = self._range
            rows = rows[lo : hi + 1]
        elif self._limit is not None:
            rows = rows[: self._limit]
        return _Result(rows)

    def _match(self, r) -> bool:
        for col, op, val in self._filters:
            rv = r.get(col)
            if op == "eq" and rv != val:
                return False
            if op == "gte" and not (rv is not None and rv >= val):
                return False
            if op == "gt" and not (rv is not None and rv > val):
                return False
            if op == "lt" and not (rv is not None and rv < val):
                return False
            if op == "in" and rv not in val:
                return False
        return True


class FakeSB:
    """store = {table_name: [row_dict, ...]}. 테스트가 초기 데이터를 주입/검사."""

    def __init__(self, store: dict | None = None):
        self.store = {k: [dict(r) for r in v] for k, v in (store or {}).items()}

    def table(self, name: str):
        return _Query(name, self.store)

    def rpc(self, name, args):
        if name == "fn_create_clip_vlm_selector_run":
            run=dict(args["p_run"]); runs=self.store.setdefault("clip_vlm_selector_runs",[])
            hit=next((r for r in runs if (r.get("camera_id"),r.get("window_start"),r.get("selector_version"))==(run.get("camera_id"),run.get("window_start"),run.get("selector_version"))),None)
            if hit is None:run["id"]=f"clip_vlm_selector_runs-{len(runs)}";runs.append(run);hit=run
            jobs=self.store.setdefault("clip_vlm_jobs",[])
            for j in args["p_jobs"]:
                if not any(x.get("clip_id")==j.get("clip_id") and x.get("selector_version")==run.get("selector_version") for x in jobs):
                    row={**j,"id":f"clip_vlm_jobs-{len(jobs)}","selector_run_id":hit["id"],"camera_id":run["camera_id"],"selector_version":run["selector_version"],"window_start":run["window_start"],"window_end":run.get("window_end"),"producer_host":run.get("producer_host"),"producer_run_id":run.get("producer_run_id"),"status":"queued","attempt_count":0,"queued_at":f"2026-07-15T00:00:{len(jobs):02d}+00:00","completed_at":None,"error_code":None};jobs.append(row)
            return _RpcResult(hit["id"])
        if name == "fn_reserve_clip_vlm_job":
            job=next(x for x in self.store.get("clip_vlm_jobs",[]) if x["id"]==args["p_job_id"]);job["status"]="submitted";job["attempt_count"]+=1;return _RpcResult(True)
        if name == "fn_claim_vlm_slack_notification":
            store=self.store.setdefault("vlm_slack_notifications",[])
            key=(args["p_selector"],args["p_window_start"],args["p_window_end"],args["p_host"])
            if any((r["selector_version"],r["window_start"],r["window_end"],r["producer_host"])==key for r in store):
                return _RpcResult(False)  # 이미 claim 됨(순차 재실행/동시 실행) → False
            store.append({"selector_version":key[0],"window_start":key[1],"window_end":key[2],"producer_host":key[3],"run_id":args.get("p_run_id")})
            return _RpcResult(True)
        if name == "fn_release_vlm_slack_notification":
            key=(args["p_selector"],args["p_window_start"],args["p_window_end"],args["p_host"])
            store=self.store.get("vlm_slack_notifications",[])
            self.store["vlm_slack_notifications"]=[r for r in store if (r["selector_version"],r["window_start"],r["window_end"],r["producer_host"])!=key]
            return _RpcResult(None)
        raise KeyError(name)

class _RpcResult:
    def __init__(self,data):self.data=data
    def execute(self):return self
