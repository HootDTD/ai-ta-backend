from __future__ import annotations

import hashlib
import os
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from backend.vendors.openai_client import generate_ai_use_markdown

# Local imports kept lightweight. Persistence is handled in routes.


REDACTION_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "<redacted>"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-_.]{20,}", re.I), r"\1<redacted>"),
    (re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"]?[A-Za-z0-9\-_.]{10,}['\"]?"), "<redacted>"),
]


def redact(text: str) -> str:
    out = text or ""
    for pat, repl in REDACTION_PATTERNS:
        out = pat.sub(repl, out)
    return out


def excerpt(text: str, limit: int = 1000) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    return t[:limit]


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha256_hex16(text: str) -> str:
    return sha256_hex(text)[:16]


def extract_file_refs(answer_text: str) -> List[str]:
    """Extract file references from markers like ``[Textbook, p. 12]`` or 'Retrieved:' lists."""
    refs: List[str] = []
    if not answer_text:
        return refs
    # citation markers like [Textbook, p. 12]
    for m in re.findall(r"\[[^,\[\]]+,\s*p\.\s*[^\]]+\]", answer_text):
        refs.append(m)
    # simple fallback: lines after 'Retrieved:' comma-separated
    m = re.search(r"Retrieved:\s*(.+)$", answer_text, re.M)
    if m:
        refs.extend([s.strip() for s in m.group(1).split(",") if s.strip()])
    # de-dup while preserving order
    seen: set[str] = set()
    out: List[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _summarize_tool_inputs(inputs: Any, limit: int = 180) -> str:
    try:
        s = json.dumps(inputs, ensure_ascii=False)
    except Exception:
        s = str(inputs)
    return excerpt(s, limit)


def _estimate_tokens(text: str) -> int:
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text or ""))
    except Exception:
        return max(1, len(text or "") // 4)


def _classify_usage_from_text(text: str) -> List[str]:
    t = (text or "").lower()
    tags: List[str] = []
    # very simple keyword rules
    if any(k in t for k in ["brainstorm", "ideas", "topic"]):
        tags.append("brainstorming")
    if any(k in t for k in ["outline", "structure", "sections"]):
        tags.append("outlining")
    if any(k in t for k in ["edit", "revise", "improve wording", "polish"]):
        tags.append("editing")
    if any(k in t for k in ["translate", "translation"]):
        tags.append("translation")
    if any(k in t for k in ["debug", "error", "traceback"]):
        tags.append("debugging")
    if any(k in t for k in ["summarise", "summarize", "tl;dr", "summary"]):
        tags.append("summarising")
    if any(k in t for k in ["code", "function", "snippet", "bug"]):
        tags.append("coding-help")
    if any(k in t for k in ["derive", "equation", "solve", "proof"]):
        tags.append("math-derivation")
    if any(k in t for k in ["clean", "csv", "data", "normalize"]):
        tags.append("data-cleaning")
    # de-dup and stable order per the closed set
    order = [
        "brainstorming",
        "outlining",
        "editing",
        "translation",
        "debugging",
        "summarising",
        "coding-help",
        "math-derivation",
        "data-cleaning",
    ]
    seen = set()
    return [x for x in order if x in tags and not (x in seen or seen.add(x))]


def classify_usage(evidence_pack: dict) -> List[str]:
    tags: List[str] = []
    for t in evidence_pack.get("turns", []):
        # Use original excerpts to classify
        tags.extend(_classify_usage_from_text(t.get("content_excerpt", "")))
    # stable unique
    order = [
        "brainstorming",
        "outlining",
        "editing",
        "translation",
        "debugging",
        "summarising",
        "coding-help",
        "math-derivation",
        "data-cleaning",
    ]
    uniq = []
    seen = set()
    for tag in order:
        if tag in tags and tag not in seen:
            uniq.append(tag)
            seen.add(tag)
    return uniq


def build_evidence_pack(
    chat_id: str,
    style: str,
    length: str,
    *,
    chat_loader: Optional[Any] = None,
) -> dict:
    """Assemble evidence for a given chat.

    chat_loader(chat_id) -> dict should return a structure like:
    {
      'chat_id': str,
      'meta': {'course_id':..., 'assignment_id':..., 'due_date': ...},
      'turns': [
         {'turn_id': 't1', 'role': 'user'|'assistant'|'tool', 'content': '...',
          'created_at': 'ISO', 'model': 'gpt-4o', 'tool_name': 'retriever',
          'tool_inputs': {...}, 'attachments': [{'name':..., 'mime':..., 'path':...}]}
      ]
    }

    If no loader is provided, a ValueError is raised (tests can inject a loader).
    """
    if chat_loader is None:
        raise ValueError("chat_loader is required to fetch chat transcript")

    raw = chat_loader(chat_id)
    if not raw or "turns" not in raw:
        raise ValueError("chat not found or malformed")

    # student/course meta
    meta = raw.get("meta") or {}
    course_meta = {
        "course_id": meta.get("course_id"),
        "assignment_id": meta.get("assignment_id"),
        "due_date": meta.get("due_date"),
    }
    student_meta = {
        "student_id": meta.get("student_id"),
        "student_name": meta.get("student_name"),
    }

    # assemble turns
    turns_out: List[dict] = []
    prompt_hashes: List[str] = []
    tool_calls: List[dict] = []
    file_refs: List[str] = []
    models_seen: List[str] = []

    for t in raw.get("turns", []):
        role = t.get("role")
        content = t.get("content", "")
        red = redact(content)
        if role == "user":
            # hash original user prompt; store short 16-hex in the pack
            prompt_hashes.append(_sha256_hex16(content))

        if role == "assistant":
            file_refs.extend(extract_file_refs(content))

        tool_name = t.get("tool_name")
        tool_inputs = t.get("tool_inputs")
        if tool_name:
            tool_calls.append(
                {
                    "name": tool_name,
                    "inputs_summary": _summarize_tool_inputs(tool_inputs),
                }
            )

        model_name = t.get("model")
        if model_name:
            models_seen.append(model_name)

        turns_out.append(
            {
                "turn_id": t.get("turn_id"),
                "role": role,
                "content_excerpt": excerpt(red, 1000),
                "created_at": t.get("created_at"),
                "model": t.get("model"),
                "tool_name": tool_name,
                "anchor": f"turn-{t.get('turn_id') or len(turns_out)+1}",
                "attachments": t.get("attachments") or [],
            }
        )

    # De-dup tool calls (same name+inputs_summary)
    seen_tc: set[Tuple[str, str]] = set()
    uniq_tool_calls: List[dict] = []
    for tc in tool_calls:
        key = (tc["name"], tc["inputs_summary"])
        if key not in seen_tc:
            seen_tc.add(key)
            uniq_tool_calls.append(tc)

    # Token budget guard: drop oldest until under budget
    budget = int(os.getenv("EVIDENCE_TOKEN_BUDGET", "8000"))
    def _serialize(ts: List[dict]) -> str:
        return "\n".join(
            f"[{x.get('role')}] {x.get('created_at','')}: {x.get('content_excerpt','')}" for x in ts
        )
    dropped = 0
    while turns_out and _estimate_tokens(_serialize(turns_out)) > budget:
        turns_out.pop(0)
        dropped += 1
    if dropped and turns_out:
        turns_out[0] = {
            **turns_out[0],
            "content_excerpt": "… [truncated] " + (turns_out[0].get("content_excerpt") or ""),
        }

    pack = {
        "chat_id": chat_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "style": style,
        "length": length,
        "course_meta": course_meta,
        "student_meta": student_meta,
        "turns": turns_out,
        "tool_calls": uniq_tool_calls,
        "file_references": list(dict.fromkeys(file_refs)),  # de-dup preserve order
        "prompt_hashes": prompt_hashes,
        "model_fingerprints": sorted(set(models_seen)),
    }
    if dropped:
        pack["truncated"] = True
        pack["truncated_count"] = dropped
    return pack


def generate_report(evidence_pack: dict, style: str, length: str) -> dict:
    """Generate a report using the OpenAI client helper.

    This integrates the vendor client which handles token budgeting,
    retries/backoff, and returns both markdown and JSON-LD artifacts.
    In tests, set TEST_FAKE_OPENAI=1 for a deterministic offline double.
    """
    out = generate_ai_use_markdown(evidence_pack, style=style, length=length)
    return {
        "markdown": out["markdown"],
        "jsonld": out["jsonld"],
        "model_fingerprint": out["model_fingerprint"],
        "tool_calls": evidence_pack.get("tool_calls", []),
        "prompt_hashes": evidence_pack.get("prompt_hashes", []),
    }
