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
        "Any code must be pure Python using only np, sp, or ureg and must print results."
    )
    bundle_json = json.dumps(asdict(bundle))
    user_base = (
        f"Task: {json.dumps(asdict(parsed_task))}\nBundle: {bundle_json}\n"
        "Return JSON with keys: steps, final_answers, equations_used, assumptions, code (optional)."
    )
    if hint:
        user_base += f"\nHint: {hint}"
    model = os.getenv("MAIN_MODEL", "gpt-4o")

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
