"""Shared in-memory fake of ``vendors.supabase_client``.

Both ``tests/conftest.py`` and ``tests/functions-tests/conftest.py`` previously
carried near-identical copies of this PostgREST-style mock. This is the single
source of truth. The one behavioural difference between the two call sites is
preserved via the ``auto_id`` flag:

  - root suite:           ``SupabaseMock()``            (insert/upsert leave ids alone)
  - functions-tests suite: ``SupabaseMock(auto_id=True)`` (insert/upsert fill id + created_at)

Each test gets a fresh instance (the autouse fixtures are function-scoped), so
state never leaks between tests.
"""
from __future__ import annotations

import uuid
from typing import Any

_SKIP_KEYS = ("select", "order", "limit", "on_conflict")


class SupabaseMock:
    def __init__(self, *, auto_id: bool = False) -> None:
        self.store: dict[str, list[dict]] = {}
        self.auto_id = auto_id

    # -- store lifecycle ----------------------------------------------------
    def reset(self) -> None:
        self.store.clear()

    # -- read ---------------------------------------------------------------
    def select(self, table: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        rows = list(self.store.get(table, []))
        for key, val in params.items():
            if key in _SKIP_KEYS:
                continue
            if isinstance(val, str) and val.startswith("eq."):
                target = val[3:]
                rows = [r for r in rows if str(r.get(key, "")) == target]
            elif isinstance(val, str) and val.startswith("lte."):
                target = int(val[4:])
                rows = [r for r in rows if r.get(key) is not None and r.get(key) <= target]
        order = params.get("order", "")
        if order:
            field = order.split(".")[0]
            desc = "desc" in order
            rows.sort(key=lambda r: r.get(field, ""), reverse=desc)
        limit = params.get("limit")
        if limit:
            rows = rows[: int(limit)]
        return rows

    def select_one(self, table: str, params: dict | None = None) -> dict | None:
        rows = self.select(table, params)
        return rows[0] if rows else None

    # -- write --------------------------------------------------------------
    def insert(self, table: str, data: Any) -> list[dict]:
        if isinstance(data, dict):
            data = [data]
        bucket = self.store.setdefault(table, [])
        for row in data:
            if self.auto_id:
                row.setdefault("id", str(uuid.uuid4()))
                row.setdefault("created_at", "2025-01-01T00:00:00Z")
            bucket.append(dict(row))
        return list(data)

    def upsert(self, table: str, data: Any, on_conflict: str = "id") -> list[dict]:
        if isinstance(data, dict):
            data = [data]
        rows = self.store.setdefault(table, [])
        for row in data:
            found = None
            for idx, existing in enumerate(rows):
                if existing.get(on_conflict) == row.get(on_conflict):
                    found = idx
                    break
            if found is None:
                if self.auto_id:
                    row.setdefault("id", str(uuid.uuid4()))
                rows.append(dict(row))
            else:
                rows[found].update(row)
        return list(data)

    def update(self, table: str, match_params: dict, data: dict) -> list[dict]:
        out = []
        for row in self.store.get(table, []):
            if self._matches(row, match_params):
                row.update(data)
                out.append(row)
        return out

    def delete(self, table: str, match_params: dict) -> None:
        self.store[table] = [
            row
            for row in self.store.get(table, [])
            if not self._matches(row, match_params)
        ]

    def rpc(self, function_name: str, params: dict, *, timeout: int = 30) -> list:
        return []

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _matches(row: dict, match_params: dict) -> bool:
        for key, val in match_params.items():
            if isinstance(val, str) and val.startswith("eq."):
                if str(row.get(key, "")) != val[3:]:
                    return False
        return True

    # -- wiring -------------------------------------------------------------
    def install(self, monkeypatch) -> None:
        """Patch ``vendors.supabase_client`` to route through this mock."""
        import vendors.supabase_client as sb_mod

        monkeypatch.setattr(sb_mod, "select", self.select)
        monkeypatch.setattr(sb_mod, "select_one", self.select_one)
        monkeypatch.setattr(sb_mod, "insert", self.insert)
        monkeypatch.setattr(sb_mod, "upsert", self.upsert)
        monkeypatch.setattr(sb_mod, "update", self.update)
        monkeypatch.setattr(sb_mod, "delete", self.delete)
        monkeypatch.setattr(sb_mod, "rpc", self.rpc)
        monkeypatch.setattr(sb_mod, "_reset", self.reset, raising=False)
