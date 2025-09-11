from __future__ import annotations

"""Wrapper functions for the user-facing agent."""

import json
import os
import re
from dataclasses import asdict
from typing import Any, List, Dict

from openai import OpenAI

from .contracts import ParsedTask, ProposedSolution, FinalAnswer, ResearchBundle
from .solver import run_python


def _client() -> OpenAI:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY not set")
    return OpenAI()


def parse_question(user_query: str) -> ParsedTask:
    """Use a lightweight model to parse the raw user query into a ``ParsedTask``."""

    client = _client()
    system = (
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
        "No browsing or outside knowledge. Every non-math statement must cite a bundle marker like [§..., p.X]. "
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
        meta = bundle.metadata or {}
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
        if meta_parts:
            lines.append("meta: " + ", ".join(meta_parts))

        for i, sn in enumerate(bundle.snippets, 1):
            marker = getattr(sn, "citation_marker", None)
            if not marker:
                marker = f"[§{sn.doc_short} • {sn.section_path}, p.{sn.page}]"
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
        marker = getattr(sn, "citation_marker", None)
        if marker is None:
            marker = getattr(sn, "marker", None)
        if marker:
            allowed.add(marker)
    seen: set[str] = set()
    cites: List[str] = []
    for m in re.findall(r"\[§[^\]]+\]", text_str):
        if m in allowed and m not in seen:
            seen.add(m)
            cites.append(m)
    return FinalAnswer(text=text_str, citations=cites)


__all__ = ["parse_question", "solve_with_bundle", "format_answer"]
