"""Shared Supabase REST client for backend modules.

Uses the publishable/anon API key for all operations.
RLS policies on the Supabase side control access per table.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

_URL: Optional[str] = None
_KEY: Optional[str] = None


def _cfg() -> tuple[str, str]:
    global _URL, _KEY
    if _URL is None:
        _URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
        _KEY = (os.environ.get("SUPABASE_API_KEY") or "").strip()
    return _URL, _KEY


def _reset() -> None:
    """Reset cached config (for testing)."""
    global _URL, _KEY
    _URL = None
    _KEY = None


def _headers(*, prefer: str = "return=representation") -> Dict[str, str]:
    _, key = _cfg()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": prefer,
    }


def _rest_url(table: str) -> str:
    url, _ = _cfg()
    return f"{url}/rest/v1/{table}"


def select(table: str, params: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """SELECT rows from a table. Returns a list of dicts."""
    r = requests.get(_rest_url(table), headers=_headers(), params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def select_one(table: str, params: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """SELECT a single row. Returns None if not found."""
    rows = select(table, params)
    return rows[0] if rows else None


def insert(table: str, data: Dict[str, Any] | List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """INSERT one or more rows. Returns the inserted rows."""
    r = requests.post(_rest_url(table), headers=_headers(), json=data, timeout=20)
    r.raise_for_status()
    return r.json()


def upsert(table: str, data: Dict[str, Any] | List[Dict[str, Any]], on_conflict: str = "id") -> List[Dict[str, Any]]:
    """UPSERT rows (insert or update on conflict)."""
    h = _headers(prefer="return=representation,resolution=merge-duplicates")
    r = requests.post(
        _rest_url(table), headers=h, json=data,
        params={"on_conflict": on_conflict}, timeout=20,
    )
    r.raise_for_status()
    return r.json()


def update(table: str, match_params: Dict[str, str], data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """UPDATE rows matching filter params."""
    r = requests.patch(_rest_url(table), headers=_headers(), params=match_params, json=data, timeout=20)
    r.raise_for_status()
    return r.json()


def delete(table: str, match_params: Dict[str, str]) -> None:
    """DELETE rows matching filter params."""
    r = requests.delete(_rest_url(table), headers=_headers(), params=match_params, timeout=20)
    r.raise_for_status()


def rpc(function_name: str, params: Dict[str, Any], *, timeout: int = 30) -> Any:
    """Call a Supabase PostgREST RPC function."""
    url, _ = _cfg()
    r = requests.post(
        f"{url}/rest/v1/rpc/{function_name}",
        headers=_headers(),
        json=params,
        timeout=timeout,
    )
    if not r.ok:
        log.error("RPC %s failed: HTTP %s – %s", function_name, r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()
