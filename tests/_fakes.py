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

    # --- terminal ---
    def execute(self):
        if self._pending is not None:
            return _Result(self._pending)
        rows = [r for r in self._store.get(self._t, []) if self._match(r)]
        if self._order:
            rows = sorted(rows, key=lambda r: r.get(self._order))
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Result(rows)

    def _match(self, r) -> bool:
        for col, op, val in self._filters:
            rv = r.get(col)
            if op == "eq" and rv != val:
                return False
            if op == "gte" and not (rv is not None and rv >= val):
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
