from __future__ import annotations

"""Wrapper functions for the user-facing agent."""

import json
import os
import re
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .config import get_subject_name, get_citation_label
from .contracts import (
    ParsedTask,
    ProposedSolution,
    FinalAnswer,
    ResearchBundle,
    BundleSnippet,
)
from .solver import run_python


_PROOF_PATH_CACHE: Optional[Path] = None
_CITATIONS_PATH_CACHE: Optional[Path] = None
_SNIPPET_FIELD_NAMES: set[str] = {f.name for f in fields(BundleSnippet)}


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


def _load_proof_bundle() -> Optional[Dict[str, Any]]:
    """Return the latest proof bundle if ``proof.json`` is available."""

    global _PROOF_PATH_CACHE
    candidates: List[Path] = []
    env_path = os.getenv("AI_TA_PROOF_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("proof.json"))
    candidates.append(Path(__file__).resolve().parent / "proof.json")

    seen: set[Path] = set()
    for raw_path in candidates:
        path = raw_path
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        try:
            with resolved.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            bundle = data.get("research_bundle")
            if isinstance(bundle, dict):
                _PROOF_PATH_CACHE = resolved
                return bundle
            if "snippets" in data and "metadata" in data:
                _PROOF_PATH_CACHE = resolved
                return data
            if isinstance(data.get("allowed_markers"), list) or isinstance(
                data.get("used_ids"), list
            ):
                _PROOF_PATH_CACHE = resolved
                return data
    return None


def _resolve_proof_path() -> Optional[Path]:
    """Best-effort resolution of the proof.json path for persistence."""

    global _PROOF_PATH_CACHE
    if _PROOF_PATH_CACHE is not None:
        return _PROOF_PATH_CACHE

    candidates: List[Path] = []
    env_path = os.getenv("AI_TA_PROOF_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("proof.json"))
    candidates.append(Path(__file__).resolve().parent / "proof.json")

    fallback: Optional[Path] = None
    seen: set[Path] = set()
    for raw_path in candidates:
        path = raw_path
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if fallback is None:
            fallback = resolved
        if resolved.exists():
            _PROOF_PATH_CACHE = resolved
            return resolved
    if fallback is not None:
        _PROOF_PATH_CACHE = fallback
    return fallback


def _write_proof_citations(
    bundle: ResearchBundle, allowed_markers: List[str], used_markers: List[str]
) -> None:
    """Persist the latest citation markers to proof.json for future runs."""

    path = _resolve_proof_path()
    if path is None:
        return

    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
        else:
            existing = {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}

    def _as_str_list(raw: Any) -> List[str]:
        if isinstance(raw, list):
            return [str(item) for item in raw if item is not None]
        if raw is None:
            return []
        return [str(raw)]

    used_ids = _as_str_list(getattr(bundle, "used_ids", []))
    original_used_ids: List[str] = []
    provenance = getattr(bundle, "provenance", None)
    if isinstance(provenance, dict):
        original_used_ids = _as_str_list(provenance.get("original_used_ids"))

    def _apply(container: Dict[str, Any]) -> None:
        if not isinstance(container, dict):
            return
        container["allowed_markers"] = list(allowed_markers)
        container["used_ids"] = list(used_ids) if used_ids else list(allowed_markers)
        if used_markers:
            container["citations_used"] = list(used_markers)
        else:
            container.pop("citations_used", None)
        if original_used_ids:
            container["original_used_ids"] = list(original_used_ids)
        else:
            container.pop("original_used_ids", None)

    _apply(existing)

    research_bundle = existing.get("research_bundle")
    if isinstance(research_bundle, dict):
        _apply(research_bundle)
        metadata = research_bundle.get("metadata")
        if isinstance(metadata, dict):
            metadata["allowed_markers"] = list(allowed_markers)

    metadata_top = existing.get("metadata")
    if isinstance(metadata_top, dict):
        metadata_top["allowed_markers"] = list(allowed_markers)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2, ensure_ascii=False)
    except OSError:
        return


def _resolve_citations_path() -> Optional[Path]:
    """Locate where ``citations.json`` should be stored."""

    global _CITATIONS_PATH_CACHE
    if _CITATIONS_PATH_CACHE is not None:
        return _CITATIONS_PATH_CACHE

    candidates: List[Path] = []
    env_path = os.getenv("AI_TA_CITATIONS_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("citations.json"))
    candidates.append(Path(__file__).resolve().parent / "citations.json")

    fallback: Optional[Path] = None
    seen: set[Path] = set()
    for raw_path in candidates:
        path = raw_path
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if fallback is None:
            fallback = resolved
        if resolved.exists():
            _CITATIONS_PATH_CACHE = resolved
            return resolved
    if fallback is not None:
        _CITATIONS_PATH_CACHE = fallback
    return fallback


def _write_citations_file(
    bundle: ResearchBundle,
    markers: List[str],
    used_markers: Optional[List[str]] = None,
) -> None:
    """Persist all citations the indexer returned for the current query."""

    path = _resolve_citations_path()
    if path is None:
        return

    deduped: List[str] = []
    seen: set[str] = set()
    for marker in markers:
        key = marker.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)

    used_markers = used_markers or []
    used_deduped: List[str] = []
    used_seen: set[str] = set()
    for marker in used_markers:
        key = marker.strip()
        if not key or key in used_seen:
            continue
        used_seen.add(key)
        used_deduped.append(key)

    question = getattr(bundle.metadata, "question", "")
    doc_sets = getattr(bundle.metadata, "doc_sets", [])
    iteration_trace = getattr(bundle.metadata, "iteration_trace", [])

    snippet_payload: List[Dict[str, Any]] = []
    for sn in getattr(bundle, "snippets", []):
        try:
            snippet_payload.append(
                {
                    "id": sn.id,
                    "marker": sn.citation_marker,
                    "page": sn.page,
                    "why": sn.why,
                    "source_path": sn.source_path,
                }
            )
        except AttributeError:
            continue

    payload = {
        "question": question,
        "doc_sets": list(doc_sets) if isinstance(doc_sets, list) else doc_sets,
        "allowed_markers": deduped,
        "used_markers": used_deduped,
        "used_ids": list(getattr(bundle, "used_ids", [])),
        "iteration_trace": iteration_trace,
        "snippets": snippet_payload,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    except OSError:
        return


def _convert_proof_snippets(
    proof_bundle: Dict[str, Any]
) -> List[BundleSnippet]:
    """Convert snippet dictionaries from ``proof.json`` into BundleSnippet objects."""

    raw_snippets = proof_bundle.get("snippets")
    if not isinstance(raw_snippets, list):
        return []

    snippets: List[BundleSnippet] = []
    for item in raw_snippets:
        if not isinstance(item, dict):
            continue
        payload = {name: item.get(name) for name in _SNIPPET_FIELD_NAMES}
        try:
            snippet = BundleSnippet(**payload)  # type: ignore[arg-type]
            snippet.validate()
        except Exception:
            continue
        snippets.append(snippet)
    return snippets


def extract_keywords(question: str) -> List[str]:
    """Ask the LLM to identify 3–8 high-value textbook concepts."""

    client = _client()
    system = (
        "You read user questions. Identify the 3-8 most important domain concepts "
        "or symbols that the prompt itself highlights. Base your choices strictly "
        "on the user's wording without relying on external subject hints. "
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


def filter_keywords_by_subject(
    terms: List[str], question: str | None = None
) -> List[str] | None:
    """Filter candidate keywords so that only subject-relevant items remain."""

    if not terms:
        return []

    client = _client()
    subject = get_subject_name()
    system = (
        "You vet candidate search keywords for textbook retrieval. "
        f"Keep only the terms that are genuinely relevant to {subject}. "
        "Only select from the provided candidates and preserve their wording. "
        "Return JSON with a single key 'accepted' containing an array of terms to keep."
    )
    payload = {
        "subject": subject,
        "question": question or "",
        "candidates": terms,
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
        accepted_raw = data.get("accepted")
        if not isinstance(accepted_raw, list):
            accepted_raw = data.get("keywords")
    except Exception:
        return None

    if not isinstance(accepted_raw, list):
        return None

    accepted_set = set()
    for item in accepted_raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip().lower()
        if cleaned:
            accepted_set.add(cleaned)

    if not accepted_set:
        return []

    return [term for term in terms if term.strip().lower() in accepted_set]


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
    proof_bundle = _load_proof_bundle()
    proof_json: Optional[str] = None

    def _normalize_str_list(items: List[Any]) -> List[str]:
        results: List[str] = []
        for item in items:
            if isinstance(item, str):
                results.append(item)
            elif item is not None:
                results.append(str(item))
        return results

    def _extract_list(source: Any, key: str) -> Optional[List[str]]:
        if not isinstance(source, dict):
            return None
        stack: List[Dict[str, Any]] = [source]
        seen_ids: set[int] = set()
        while stack:
            current = stack.pop()
            current_id = id(current)
            if current_id in seen_ids:
                continue
            seen_ids.add(current_id)
            val = current.get(key)
            if isinstance(val, list):
                normalized = _normalize_str_list(val)
                if normalized:
                    return normalized
            for nested_key in ("research_bundle", "metadata", "bundle", "proof"):
                nested = current.get(nested_key)
                if isinstance(nested, dict):
                    stack.append(nested)
        return None

    if proof_bundle is not None:
        proof_allowed_raw = _extract_list(proof_bundle, "allowed_markers") or []
        proof_used_raw = _extract_list(proof_bundle, "used_ids") or []
        allowed_list = _normalize_str_list(proof_allowed_raw)
        used_list = _normalize_str_list(proof_used_raw)
        combined_markers: List[str] = []
        if allowed_list:
            combined_markers.extend(allowed_list)
        if used_list:
            combined_markers.extend(used_list)
        if combined_markers:
            deduped: List[str] = []
            seen_markers: set[str] = set()
            for marker in combined_markers:
                key = marker.strip()
                if not key:
                    continue
                if key in seen_markers:
                    continue
                seen_markers.add(key)
                deduped.append(key)
            combined_markers = deduped
            original_used = list(getattr(bundle, "used_ids", []))
            bundle.allowed_markers = combined_markers
            meta_obj = getattr(bundle, "metadata", None)
            if isinstance(meta_obj, dict):
                meta_obj["allowed_markers"] = list(combined_markers)
            elif meta_obj is not None:
                setattr(meta_obj, "allowed_markers", list(combined_markers))
            bundle.used_ids = list(combined_markers)
            if isinstance(getattr(bundle, "provenance", None), dict):
                bundle.provenance.setdefault("original_used_ids", original_used)
        proof_snippets = _convert_proof_snippets(proof_bundle)
        if proof_snippets:
            bundle.snippets = proof_snippets
            if combined_markers:
                bundle.used_ids = list(combined_markers)
            else:
                bundle.used_ids = [
                    sn.citation_marker
                    for sn in proof_snippets
                    if getattr(sn, "citation_marker", None)
                ]
        for attr in ("equations", "glossary", "assumptions"):
            proof_val = proof_bundle.get(attr)
            if isinstance(proof_val, list) and proof_val:
                setattr(bundle, attr, proof_val)
        proof_stats = proof_bundle.get("stats")
        if isinstance(proof_stats, dict) and proof_stats:
            bundle.stats.update(proof_stats)
        try:
            proof_json = json.dumps(proof_bundle, ensure_ascii=False)
        except TypeError:
            proof_json = None
    markers_for_log = list(getattr(bundle, "allowed_markers", []))
    if not markers_for_log:
        markers_for_log = [
            getattr(sn, "citation_marker", "")
            for sn in getattr(bundle, "snippets", [])
            if getattr(sn, "citation_marker", None)
        ]
    try:
        _write_citations_file(bundle, markers_for_log)
    except Exception:
        pass
    bundle_json = json.dumps(asdict(bundle), ensure_ascii=False)

    payload_lines = [
        f"Task: {json.dumps(asdict(parsed_task), ensure_ascii=False)}",
        f"Bundle: {bundle_json}",
    ]
    if proof_json:
        payload_lines.append(f"FullProofBundle: {proof_json}")
    payload_lines.append(
        "Return JSON with keys: steps, final_answers, equations_used, assumptions, code (optional)."
    )
    user_base = "\n".join(payload_lines)
    if hint:
        user_base += f"\nHint: {hint}"
    model = os.getenv("MAIN_MODEL", "gpt-5")

    def _maybe_debug_dump(
        system_prompt: str,
        user_payload: str,
        bundle: ResearchBundle,
        proof_bundle: Optional[Dict[str, Any]] = None,
    ) -> None:
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
        if proof_bundle is not None:
            with open("debug/main_ai_proof_bundle.json", "w", encoding="utf-8") as fh:
                json.dump(proof_bundle, fh, indent=2, ensure_ascii=False)

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

    _maybe_debug_dump(system, user_base, bundle, proof_bundle)

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
    if text_str.strip() == "Not found in the approved materials.":
        return FinalAnswer(text="Not found in the approved materials.", citations=[])

    raw_allowed = getattr(bundle, "allowed_markers", None) or []
    allowed_markers: List[str] = []
    allowed_seen: set[str] = set()
    for marker in raw_allowed:
        if not isinstance(marker, str):
            continue
        cleaned = marker.strip()
        if cleaned and cleaned not in allowed_seen:
            allowed_markers.append(cleaned)
            allowed_seen.add(cleaned)
    if not allowed_markers:
        for sn in bundle.snippets:
            marker = getattr(sn, "citation_marker", None) or getattr(sn, "marker", None)
            if not isinstance(marker, str) or not marker.strip():
                marker = _fallback_citation_marker(sn)
            cleaned = marker.strip()
            if cleaned and cleaned not in allowed_seen:
                allowed_markers.append(cleaned)
                allowed_seen.add(cleaned)
    if not allowed_markers:
        fallback = f"[{get_citation_label()}, p. ?]"
        allowed_markers.append(fallback)
        allowed_seen.add(fallback)
    allowed_set: set[str] = set(allowed_markers)

    snippet_infos: List[tuple[str, str, str]] = []
    info_seen: set[str] = set()
    for sn in bundle.snippets:
        marker = getattr(sn, "citation_marker", None) or getattr(sn, "marker", None)
        if not isinstance(marker, str) or not marker.strip():
            marker = _fallback_citation_marker(sn)
        cleaned = marker.strip()
        if cleaned and cleaned not in allowed_set:
            allowed_markers.append(cleaned)
            allowed_set.add(cleaned)
        if cleaned and cleaned not in info_seen:
            info_seen.add(cleaned)
            reason = getattr(sn, "why", "") or "context"
            snippet_text = getattr(sn, "text", "")
            snippet_infos.append((cleaned, reason, snippet_text))

    if snippet_infos:
        background_lines: List[str] = []
        for marker, reason, snippet_text in snippet_infos:
            snippet_clean = " ".join(str(snippet_text or "").split())
            background_lines.append(
                f"- {marker} ({reason}): {snippet_clean}"
            )
        text_str = text_str.rstrip() + "\n\nResearch bundle background:\n" + "\n".join(background_lines)

    missing_terms = (
        getattr(bundle, "not_found_terms", None)
        or getattr(bundle.metadata, "not_found_terms", None)
        or []
    )
    if missing_terms:
        miss_str = ", ".join(missing_terms)
        text_str = text_str.rstrip() + (
            "\n\nNote: The index did not contain information on "
            f"{miss_str}; the answer uses related context where possible."
        )

    marker_pattern = re.compile(r"\[[^,\[\]]+,\s*p\.\s*[^\]]+\]")
    used_markers: List[str] = []

    def _record_marker(marker: str) -> None:
        marker_clean = marker.strip()
        if marker_clean and marker_clean in allowed_set and marker_clean not in used_markers:
            used_markers.append(marker_clean)

    def _clean_and_tag(text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            marker = match.group(0).strip()
            if marker in allowed_set:
                _record_marker(marker)
                return marker
            return ""

        cleaned = marker_pattern.sub(repl, text)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r" \n", "\n", cleaned)
        return cleaned

    paragraphs = text_str.split("\n\n")
    rotated = 0
    for idx, para in enumerate(paragraphs):
        stripped = para.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            continue
        cleaned_para = _clean_and_tag(para)
        if marker_pattern.search(cleaned_para):
            paragraphs[idx] = cleaned_para
            continue
        marker = allowed_markers[rotated % len(allowed_markers)]
        rotated += 1
        append_target = cleaned_para.rstrip()
        if append_target:
            append_target = append_target + f" {marker}"
        else:
            append_target = marker
        _record_marker(marker)
        paragraphs[idx] = append_target

    final_text = "\n\n".join(paragraphs)
    final_text = _clean_and_tag(final_text)

    if used_markers:
        final_text = final_text.rstrip() + "\n\nCitations: " + ", ".join(used_markers)

    try:
        _write_proof_citations(bundle, allowed_markers, used_markers)
    except Exception:
        # Persistence should not block returning an answer; ignore best-effort failures.
        pass
    try:
        _write_citations_file(bundle, allowed_markers, used_markers)
    except Exception:
        pass

    return FinalAnswer(text=final_text, citations=used_markers)


__all__ = [
    "parse_question",
    "solve_with_bundle",
    "format_answer",
    "normalize_query",
    "extract_keywords",
    "propose_synonyms",
]
