from __future__ import annotations

"""Wrapper functions for the user-facing agent."""

import json
import os
import re
from dataclasses import asdict
from typing import Any, List, Dict

from openai import OpenAI

from .config import get_subject_name, get_citation_label
from .contracts import ParsedTask, ProposedSolution, FinalAnswer, ResearchBundle
from .solver import run_python


def _fallback_citation_marker(snippet: Any) -> str:
    """Produce a default citation marker when one is missing."""

    label = get_citation_label()
    page_val = getattr(snippet, "page", None)
    page = page_val if isinstance(page_val, int) and page_val > 0 else "?"
    return f"[{label}, p. {page}]"


def normalize_query(text: str) -> str:
    """Normalize a raw user query for retrieval.

    This step strips curly quotes/apostrophes, removes all quote characters,
    collapses repeated whitespace, normalizes dashes to ``-`` and lowers the
    string. The original text should be retained separately for display but the
    sanitized form is suitable for lexical probing and full‑text search without
    causing FTS hiccups.
    """

    if not text:
        return ""

    # Replace curly quotes/apostrophes with straight equivalents
    replacements = {
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
    }
    for k, v in replacements.items():
        text = text.replace(k, v)

    # Drop any remaining quote characters which can confuse FTS
    text = text.replace("'", "").replace('"', "")

    # Unify hyphens / dashes
    text = re.sub(r"[–—−]", "-", text)

    # Collapse repeated whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text.lower()


def _client() -> OpenAI:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY not set")
    return OpenAI()


def extract_keywords(question: str) -> List[str]:
    """Ask the LLM to identify 3–8 high-value textbook concepts."""

    client = _client()
    subject = get_subject_name()
    system = (
        f"You read {subject} textbook questions. "
        "Identify the 3-8 most important domain concepts or symbols that require "
        "textbook knowledge. Focus on nouns, formal terms, or symbols. "
        "Return JSON with a single key 'keywords' whose value is an ordered array."
    )
    payload = {
        "prompt": question,
    }
    try:
        resp = client.chat.completions.create(
            model=os.getenv("PARSER_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        raw = data.get("keywords", [])
    except Exception:
        raw = []

    keywords: List[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        keywords.append(cleaned)
        if len(keywords) >= 8:
            break

    if not keywords and question.strip():
        keywords = [question.strip().lower()]
    return keywords


def propose_synonyms(
    terms: List[str], context_hint: Dict[str, Any] | None = None
) -> Dict[str, List[str]]:
    """Ask the LLM to generate 1–2 plausible synonyms per term."""

    if not terms:
        return {}

    client = _client()
    subject = get_subject_name()
    system = (
        "You help with textbook lookup. For each concept term, propose up to two "
        f"alternate keywords, abbreviations, or symbols that might appear in {subject} materials. "
        "Return a JSON object mapping each input term to an array of 0-2 strings."
    )
    hint = context_hint or {}
    try:
        resp = client.chat.completions.create(
            model=os.getenv("PARSER_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"terms": terms, "context": hint}, ensure_ascii=False
                    ),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
    except Exception:
        data = {}

    mapping: Dict[str, Any] = {}
    if isinstance(data, dict):
        if "synonyms" in data and isinstance(data["synonyms"], dict):
            mapping = data["synonyms"]
        else:
            mapping = data

    suggestions: Dict[str, List[str]] = {}
    for term in terms:
        raw_list = None
        if isinstance(mapping, dict):
            for key, value in mapping.items():
                if not isinstance(value, list):
                    continue
                if key == term:
                    raw_list = value
                    break
                if isinstance(key, str) and key.lower() == term.lower():
                    raw_list = value
        cleaned_list: List[str] = []
        seen_syn: set[str] = set()
        if raw_list:
            for cand in raw_list:
                if not isinstance(cand, str):
                    continue
                cleaned = cand.strip()
                if not cleaned:
                    continue
                key_lower = cleaned.lower()
                if key_lower in seen_syn:
                    continue
                seen_syn.add(key_lower)
                cleaned_list.append(cleaned)
                if len(cleaned_list) >= 2:
                    break
        if cleaned_list:
            suggestions[term] = cleaned_list
    return suggestions


def parse_question(user_query: str) -> ParsedTask:
    """Use a lightweight model to parse the raw user query into a ``ParsedTask``."""

    client = _client()
    subject = get_subject_name()
    system = (
        f"You are parsing {subject} textbook problems. "
        "Extract problem_type, asked_outputs, knowns, constraints, and figure_refs. "
        "Return ONLY JSON with keys: problem_type, asked_outputs, knowns, constraints, figure_refs."
    )
    model = os.getenv("PARSER_MODEL", "gpt-4o-mini")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user_query}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:  # pragma: no cover - deterministic
        raise RuntimeError("Parser returned non-JSON output") from exc
    task = ParsedTask(**data)

    def _to_list(val: Any) -> List[str]:
        if isinstance(val, list):
            return val
        if isinstance(val, str) and val.strip():
            return [val]
        return []

    def _to_dict(val: Any) -> Dict[str, Any]:
        return val if isinstance(val, dict) else {}

    task.asked_outputs = _to_list(getattr(task, "asked_outputs", []))
    task.constraints = _to_list(getattr(task, "constraints", []))
    task.figure_refs = _to_list(getattr(task, "figure_refs", []))
    task.knowns = _to_dict(getattr(task, "knowns", {}))

    # Heuristic extraction for asked outputs and machine keys.
    def _extract_outputs(text: str) -> tuple[List[str], List[str]]:
        outs: List[str] = []
        keys: List[str] = []
        for raw in re.split(r"[;\n]", text):
            cleaned = raw.strip().strip("-: ")
            if not cleaned:
                continue
            cleaned = re.sub(r"^[\d\.\)]+\s*", "", cleaned)
            m = re.search(r"\b([A-Za-z][A-Za-z0-9_]*)\b", cleaned)
            if m:
                keys.append(m.group(1))
            outs.append(cleaned)
        return outs, keys

    if not task.asked_outputs:
        m = re.search(r"asked\s*outputs?:", user_query, re.IGNORECASE)
        if m:
            rest = user_query[m.end():]
            n = re.search(r"\n\s*(Knowns?:|Constraints?:|Figure|$)", rest, re.IGNORECASE)
            span = rest[: n.start() if n else len(rest)]
            outs, keys = _extract_outputs(span)
            if outs:
                task.asked_outputs = outs
                task.asked_output_keys = keys
    elif len(task.asked_outputs) == 1 and re.search(r"[;\n]", task.asked_outputs[0]):
        outs, keys = _extract_outputs(task.asked_outputs[0])
        task.asked_outputs = outs
        task.asked_output_keys = keys

    if not getattr(task, "asked_output_keys", []):
        _, keys = _extract_outputs("\n".join(task.asked_outputs))
        task.asked_output_keys = keys

    task.validate()
    return task


def solve_with_bundle(
    parsed_task: ParsedTask, bundle: ResearchBundle, hint: str | None = None
) -> ProposedSolution:
    """Solve the parsed task using only information from the provided bundle."""

    client = _client()
    system = (
        "You are a strict reasoning agent. Use ONLY facts in the Research Bundle. "
        "No browsing or outside knowledge. Every non-math statement must cite a bundle marker like [Textbook, p. X]. "
        "If information is missing, reply exactly 'Not found in the approved materials.' "
        "Any code must be pure Python using only np, sp, or ureg and must print results. "
        "You must include final_answers as a JSON object whose keys exactly match the asked outputs (e.g., v_exit, Q). "
        "Each value must be a number with SI units as a parsable string (e.g., '9.90 m/s'). "
        "If math is needed, include a code field (pure Python using np/sp/ureg) that prints the results."
    )
    bundle_json = json.dumps(asdict(bundle))
    user_base = (
        f"Task: {json.dumps(asdict(parsed_task))}\nBundle: {bundle_json}\n"
        "Return JSON with keys: steps, final_answers, equations_used, assumptions, code (optional)."
    )
    if hint:
        user_base += f"\nHint: {hint}"
    model = os.getenv("MAIN_MODEL", "gpt-4o")

    def _maybe_debug_dump(system_prompt: str, user_payload: str, bundle: ResearchBundle) -> None:
        def _flag_enabled() -> bool:
            truthy = {"1", "true", "yes", "on"}
            for name in ("QA_DEBUG", "AI_TA_DEBUG", "TRACE_IO"):
                val = os.getenv(name)
                if val and val.lower() in truthy:
                    return True
            return False

        if not _flag_enabled():
            return

        os.makedirs("debug", exist_ok=True)
        with open("debug/main_ai_system.txt", "w", encoding="utf-8") as fh:
            fh.write(system_prompt)
        with open("debug/main_ai_user.txt", "w", encoding="utf-8") as fh:
            fh.write(user_payload)

        lines: List[str] = []
        meta_obj = bundle.metadata
        meta = asdict(meta_obj) if not isinstance(meta_obj, dict) else meta_obj
        meta_parts: List[str] = []
        if meta.get("k_sem") is not None:
            meta_parts.append(f"k_sem={meta.get('k_sem')}")
        if meta.get("k_lex") is not None:
            meta_parts.append(f"k_lex={meta.get('k_lex')}")
        if meta.get("token_budget") is not None:
            meta_parts.append(f"token_budget={meta.get('token_budget')}")
        if meta.get("loaded_indexes") is not None:
            meta_parts.append(f"loaded_indexes={meta.get('loaded_indexes')}")
        if meta.get("skipped_indexes") is not None:
            meta_parts.append(f"skipped_indexes={meta.get('skipped_indexes')}")
        trace = meta.get("iteration_trace", [])
        if trace:
            meta_parts.append(f"iters={len(trace)}")
            for t in trace:
                repl = t.get("replaced_terms") or {}
                if repl:
                    rep_str = ",".join(f"{k}->{v}" for k, v in repl.items())
                    meta_parts.append(f"iter{t.get('iter')}: {rep_str}")
        if meta.get("missing_terms"):
            meta_parts.append("missing=" + ",".join(meta.get("missing_terms", [])))
        if meta.get("aliases_used"):
            alias_str = ",".join(meta.get("aliases_used", {}).keys())
            meta_parts.append("used_aliases=" + alias_str)
        if meta.get("expansion_plan"):
            plan_parts = [
                f"{p.get('type')}: {','.join(p.get('terms', []))} ({p.get('hit_count',0)})"
                for p in meta.get("expansion_plan", [])
            ]
            meta_parts.append("plan=" + " | ".join(plan_parts))
        if meta.get("original_query"):
            meta_parts.append(f"orig_q={meta['original_query']!r}")
        if meta.get("final_query"):
            meta_parts.append(f"final_q={meta['final_query']!r}")
        if meta_parts:
            lines.append("meta: " + ", ".join(meta_parts))

        for i, sn in enumerate(bundle.snippets, 1):
            marker = getattr(sn, "citation_marker", None)
            if not marker:
                marker = _fallback_citation_marker(sn)
            lines.append(f"S{i} {marker} why={sn.why} id={sn.id}")
            text = sn.text.replace("\n", " ")
            if len(text) > 240:
                text = text[:240].rstrip() + "…"
            lines.append(text)

        coverage = meta.get("coverage_gaps") or getattr(bundle, "coverage_gaps", None)
        if coverage:
            lines.append("coverage_gaps: " + ", ".join(coverage))
        with open("debug/main_ai_context_preview.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

        with open("debug/main_ai_bundle.json", "w", encoding="utf-8") as fh:
            json.dump(asdict(bundle), fh, indent=2, ensure_ascii=False)

        print("Wrote main model inputs to debug/main_ai_system.txt and debug/main_ai_user.txt")
        print(f"Context preview: debug/main_ai_context_preview.txt (snippets={len(bundle.snippets)})")

    # --- NEW: tell the model the exact keys to use in final_answers ---
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    asked_keys = list(parsed_task.asked_output_keys or [])
    if not asked_keys:
        asked_keys = [_slug(x) for x in (parsed_task.asked_outputs or []) if isinstance(x, str) and x.strip()]
    if asked_keys:
        key_str = ", ".join(asked_keys)
        user_base += (
            "\nRequired final_answers keys (EXACT): "
            f"[{key_str}]. Return numeric values with SI units."
        )

    _maybe_debug_dump(system, user_base, bundle)

    def _chat(msgs: List[dict]) -> dict:
        kwargs = {
            "model": model,
            "messages": msgs,
            "response_format": {"type": "json_object"},
        }
        gpt5_allow = {"gpt-5", "gpt-5-chat-latest", "gpt-5-mini"}
        if not (model.startswith("gpt-5") or model in gpt5_allow):
            kwargs["temperature"] = 0
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        return json.loads(content)

    data = _chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user_base},
    ])

    code_out = None
    code_hash = None
    vars_created: List[str] = []
    code = data.get("code")
    if isinstance(code, str):
        try:
            res = run_python(code)
            code_out = res.stdout
            code_hash = res.code_hash
            vars_created = res.vars_created
        except Exception as exc:
            err = str(exc)
            data = _chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_base},
                    {"role": "assistant", "content": json.dumps(data)},
                    {"role": "user", "content": f"Code execution error: {err}"},
                ]
            )
            code = data.get("code")
            if isinstance(code, str):
                try:
                    res = run_python(code)
                    code_out = res.stdout
                    code_hash = res.code_hash
                    vars_created = res.vars_created
                except Exception as exc2:  # pragma: no cover - best effort
                    code_out = f"error: {exc2}"

    return ProposedSolution(
        steps=data.get("steps", ""),
        final_answers=data.get("final_answers", {}),
        equations_used=data.get("equations_used", []),
        assumptions=data.get("assumptions", []),
        code=code,
        code_output=code_out,
        code_hash=code_hash,
        vars_created=vars_created,
    )


def format_answer(solution: ProposedSolution, bundle: ResearchBundle) -> FinalAnswer:
    """Format the final answer for the user without adding new facts."""
    text = getattr(solution, "final_text", None)
    if text is None:
        text = getattr(solution, "text", None)
    if text is None:
        text = solution.steps

    def _to_str(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, list):
            parts: List[str] = []
            for elem in val:
                if isinstance(elem, str):
                    parts.append(elem)
                else:
                    try:
                        parts.append(json.dumps(elem, ensure_ascii=False))
                    except Exception:
                        parts.append(str(elem))
            return "\n".join(parts)
        return str(val)

    text_str = _to_str(text)
    fa = getattr(solution, "final_answers", {}) or {}
    if isinstance(fa, dict) and fa:
        results_lines = [f"- {k} = {v}" for k, v in fa.items()]
        text_str = text_str.rstrip() + "\n\nResults:\n" + "\n".join(results_lines)
    allowed: set[str] = set()
    for sn in bundle.snippets:
        marker = getattr(sn, "citation_marker", None) or getattr(sn, "marker", None)
        if not marker:
            marker = _fallback_citation_marker(sn)
        if isinstance(marker, str) and marker.strip():
            allowed.add(marker.strip())
    seen: set[str] = set()
    cites: List[str] = []
    marker_pattern = re.compile(r"\[[^,\[\]]+,\s*p\.\s*[^\]]+\]")
    for m in marker_pattern.findall(text_str):
        m_clean = m.strip()
        if m_clean in allowed and m_clean not in seen:
            seen.add(m_clean)
            cites.append(m_clean)
    missing = getattr(bundle.metadata, "not_found_terms", []) or []
    if missing:
        miss_str = ", ".join(missing)
        text_str = text_str.rstrip() + (
            "\n\nNote: The index did not contain information on "
            f"{miss_str}; the answer uses related context where possible."
        )
    return FinalAnswer(text=text_str, citations=cites)


__all__ = [
    "parse_question",
    "solve_with_bundle",
    "format_answer",
    "normalize_query",
    "extract_keywords",
    "propose_synonyms",
]
