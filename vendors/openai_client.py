from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass
class _RetryConfig:
    max_retries: int = 5
    base_delay: float = 0.5  # seconds
    max_delay: float = 8.0   # seconds
    total_timeout: float = 30.0  # seconds


def _estimate_tokens(text: str) -> int:
    """Rough token estimate. Falls back if tiktoken isn't available."""
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text or ""))
    except Exception:
        # heuristic ~4 chars per token
        return max(1, len(text or "") // 4)


def _budget_evidence(evidence_pack: Dict[str, Any], token_budget: int = 6000) -> Tuple[Dict[str, Any], bool]:
    """Return (possibly) truncated evidence to fit within token budget.

    We primarily budget on the serialized turns portion, dropping oldest turns
    until under budget. Marks truncation with a note in the evidence.
    """
    turns: List[Dict[str, Any]] = list(evidence_pack.get("turns") or [])
    static_part = evidence_pack.copy()
    static_part.pop("turns", None)

    def serialized_len(ts: List[Dict[str, Any]]) -> int:
        try:
            turns_text = "\n".join(
                f"[{t.get('role')}] {t.get('created_at','')}: {t.get('content_excerpt','')}" for t in ts
            )
        except Exception:
            turns_text = json.dumps(ts, ensure_ascii=False)
        other_text = json.dumps(static_part, ensure_ascii=False)
        return _estimate_tokens(other_text + "\n" + turns_text)

    truncated = False
    while turns and serialized_len(turns) > token_budget:
        turns.pop(0)
        truncated = True

    if truncated and turns:
        # Prepend a visible truncation marker to the first remaining excerpt
        first = dict(turns[0])
        first["content_excerpt"] = "… [truncated] " + (first.get("content_excerpt") or "")
        turns[0] = first

    out = dict(evidence_pack)
    out["turns"] = turns
    if truncated:
        out["truncated"] = True
    return out, truncated


def _sha256_hex16(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _call_openai(messages: List[Dict[str, str]], *, response_json: bool, retry: _RetryConfig) -> Dict[str, Any] | str:
    """Call OpenAI Chat Completions with retries and timeout.

    If response_json is True, returns parsed JSON object; else returns string.
    """
    start = time.monotonic()
    attempt = 0
    last_err: Exception | None = None

    # Lazy import to avoid hard dependency during tests
    from openai import OpenAI  # type: ignore

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = OpenAI()

    while attempt < retry.max_retries and (time.monotonic() - start) < retry.total_timeout:
        try:
            kwargs: Dict[str, Any] = {
                "model": os.getenv("REPORTS_MODEL", "gpt-4o-mini"),
                "messages": messages,
                "temperature": 0.2,
            }
            if response_json:
                kwargs["response_format"] = {"type": "json_object"}

            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            if response_json:
                try:
                    return json.loads(content)
                except Exception as ex:
                    raise RuntimeError("Model did not return valid JSON") from ex
            return content
        except Exception as e:  # retry on any transient failure
            last_err = e
            attempt += 1
            # exponential backoff with jitter
            delay = min(retry.max_delay, retry.base_delay * (2 ** (attempt - 1)))
            delay *= (0.8 + 0.4 * random.random())
            # ensure we honor total timeout
            remaining = retry.total_timeout - (time.monotonic() - start)
            if remaining <= 0:
                break
            time.sleep(min(delay, max(0.05, remaining)))

    assert last_err is not None
    raise last_err


def generate_ai_use_markdown(evidence_pack: dict, style: str, length: str) -> dict:
    """
    Returns: {
      "markdown": str,
      "jsonld": dict,
      "model_fingerprint": str
    }
    """
    # Test double for CI/local tests without network
    if os.getenv("TEST_FAKE_OPENAI") == "1":
        turns = evidence_pack.get("turns") or []
        md = (
            f"# AI-use Report (fake)\n\n"
            f"Style: {style}\n\nLength: {length}\n\nTurns: {len(turns)}\n"
        )
        jsonld = {
            "@context": "https://schema.org",
            "@type": "Report",
            "name": "AI-use Report (fake)",
            "about": {"@type": "CreativeWork", "identifier": evidence_pack.get("chat_id")},
        }
        return {
            "markdown": md,
            "jsonld": jsonld,
            "model_fingerprint": _sha256_hex16(md),
        }

    # Token budget and truncation
    budget = int(os.getenv("REPORTS_TOKEN_BUDGET", "6000"))
    budgeted, truncated = _budget_evidence(evidence_pack, token_budget=budget)

    system = (
        "You are an academic integrity assistant. Write a clear, factual AI-use report based strictly on the EVIDENCE JSON. "
        "Use concise, neutral language. Do not invent facts."
    )

    user_markdown = {
        "task": "produce_markdown",
        "style": style,
        "length": length,
        "truncated": truncated,
        "evidence": budgeted,
    }

    # Ask for both artifacts in JSON to ensure reliable parsing
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "Return a JSON object with keys 'markdown' and 'jsonld'.\n"
                "- 'markdown': a human-readable report.\n"
                "- 'jsonld': structured JSON-LD summary (schema.org Report).\n"
                f"EVIDENCE JSON:\n{json.dumps(user_markdown, ensure_ascii=False)}"
            ),
        },
    ]

    retry = _RetryConfig()
    result = _call_openai(messages, response_json=True, retry=retry)
    if not isinstance(result, dict) or "markdown" not in result or "jsonld" not in result:
        raise RuntimeError("Model response missing required keys")

    markdown = str(result.get("markdown") or "")
    jsonld = result.get("jsonld") or {}
    return {
        "markdown": markdown,
        "jsonld": jsonld,
        "model_fingerprint": _sha256_hex16(markdown),
    }

