from __future__ import annotations

"""Wrapper functions for the user-facing agent."""

import json
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

from openai import OpenAI

from config.settings import get_subject_name, get_citation_label, get_runtime_dir
from config.contracts import ParsedTask, ProposedSolution, FinalAnswer, ResearchBundle
from .solver import run_python
from .prompts import (
    relevance_guard_prompt,
    score_and_answer_snippet_prompt,
    extract_keywords_prompt,
    keyword_generation_prompt,
    keyword_scoring_prompt,
    general_term_filter_prompt,
    synonyms_prompt,
    concept_extraction_prompt,
    parse_question_prompt,
    tutor_prompt,
)


def _fallback_citation_marker(snippet: Any, citation_label: str | None = None) -> str:
    """Produce a default citation marker when one is missing."""

    label = citation_label or get_citation_label()
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


def check_question_relevance(
    question: str, subject: str | None = None
) -> Dict[str, str]:
    """Use the LLM to classify question relevance as full, partial, or none.

    Returns a dict with keys: relevance, on_topic_portion, off_topic_portion, reason.
    """

    cleaned = (question or "").strip()
    if not cleaned:
        return {
            "relevance": "none",
            "on_topic_portion": "",
            "off_topic_portion": "",
            "reason": "empty question",
        }

    client = _client()
    subject = subject or get_subject_name()
    system = relevance_guard_prompt(subject)
    payload = {
        "subject": subject,
        "question": cleaned,
    }

    fail_open: Dict[str, str] = {
        "relevance": "full",
        "on_topic_portion": cleaned,
        "off_topic_portion": "",
        "reason": "guard failed — defaulting to full",
    }

    try:
        resp = client.chat.completions.create(
            model=os.getenv("PARSER_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)

        # Handle new graduated format
        relevance = data.get("relevance", "")
        if isinstance(relevance, str) and relevance in {"full", "partial", "none"}:
            log.debug("Relevance classification: %s", relevance)
            return {
                "relevance": relevance,
                "on_topic_portion": str(data.get("on_topic_portion", "")),
                "off_topic_portion": str(data.get("off_topic_portion", "")),
                "reason": str(data.get("reason", "")),
            }

        # Backward compatibility: old binary format {"relevant": bool}
        relevant = data.get("relevant")
        if isinstance(relevant, bool):
            mapped = "full" if relevant else "none"
            log.debug("Relevance (legacy bool): %s -> %s", relevant, mapped)
            return {
                "relevance": mapped,
                "on_topic_portion": cleaned if relevant else "",
                "off_topic_portion": "" if relevant else cleaned,
                "reason": str(data.get("reason", "")),
            }
        if isinstance(relevant, str):
            lowered = relevant.strip().lower()
            if lowered in {"true", "yes", "y"}:
                return {
                    "relevance": "full",
                    "on_topic_portion": cleaned,
                    "off_topic_portion": "",
                    "reason": str(data.get("reason", "")),
                }
            if lowered in {"false", "no", "n"}:
                return {
                    "relevance": "none",
                    "on_topic_portion": "",
                    "off_topic_portion": cleaned,
                    "reason": str(data.get("reason", "")),
                }
    except Exception:
        log.error("Subject relevance guard failed", exc_info=True)
    # Fail open so that legitimate questions are not blocked if the guard fails.
    return fail_open


def is_question_subject_relevant(question: str, subject: str | None = None) -> bool:
    """Use the LLM to decide whether the question belongs to the active subject.

    Thin backward-compatible wrapper around check_question_relevance.
    Returns True for both 'full' and 'partial' relevance.
    """
    result = check_question_relevance(question, subject)
    return result["relevance"] != "none"


_cached_client: Optional[OpenAI] = None


def _client() -> OpenAI:
    global _cached_client
    if _cached_client is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY not set")
        _cached_client = OpenAI()
    return _cached_client


def _load_proof_bundle() -> Optional[Dict[str, Any]]:
    """Return the latest proof bundle if ``proof.json`` is available."""

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
                return bundle
            if "snippets" in data and "metadata" in data:
                return data
            if isinstance(data.get("allowed_markers"), list) or isinstance(
                data.get("used_ids"), list
            ):
                return data
    return None


def _clamp_0_1(value: Any, default: float = 0.0) -> float:
    """Clamp a value into ``[0, 1]`` with basic type/NaN guards."""

    try:
        val = float(value)
    except (TypeError, ValueError):
        log.debug("_clamp_0_1 conversion failed for %r", value)
        return default
    if not math.isfinite(val):
        return default
    if val < 0:
        return 0.0
    if val > 1:
        return 1.0
    return val


def _importance_from_snippet(snippet: Any) -> float:
    """Return a normalized importance weight stored on a snippet."""

    fs = getattr(snippet, "final_score", {}) or {}
    raw_importance = fs.get("importance", fs.get("weight"))
    if raw_importance is None:
        return 1.0
    try:
        val = float(raw_importance)
    except (TypeError, ValueError):
        log.debug("importance conversion failed for %r", raw_importance)
        return 1.0
    if not math.isfinite(val):
        return 1.0
    # Preserve existing weighting convention from retrieval (_clamp_weight)
    return float(max(0.05, min(1.0, val)))


def _pick_concept_term(snippet: Any) -> str:
    """Return the term already associated with this citation (no new terms)."""

    terms = getattr(snippet, "concept_terms", None) or []
    if isinstance(terms, list):
        for term in terms:
            if isinstance(term, str):
                cleaned = term.strip()
                if cleaned:
                    return cleaned
    keys = getattr(snippet, "concept_keys", None) or []
    if isinstance(keys, list):
        for term in keys:
            if isinstance(term, str):
                cleaned = term.strip()
                if cleaned:
                    return cleaned
    return ""


def _citation_pool_size(n_snippets: int) -> int:
    """Thread pool width for parallel snippet scoring.

    Default cap 24 > default snippet count (K_SEM=20) so all snippets score
    in a single wave — wall time ≈ slowest single call instead of two waves.
    """
    cap = int(os.getenv("CITATION_WORKERS", "24"))
    return max(1, min(cap, n_snippets))


def _score_and_answer_snippet(
    question: str,
    snippet: Any,
    importance: float,
    focus_term: str,
    citation_label: str | None = None,
    model: str | None = None,
) -> Dict[str, Any]:
    """Score relevance AND extract answer from a single snippet in one LLM call."""
    client = _client()
    # Default stays gpt-4o: measured A/B showed gpt-4o-mini is ~30% slower
    # per call with identical scores, and parallel scoring's wall time is the
    # slowest call. CITATION_SCORER_MODEL is the single override knob.
    model = model or os.getenv("CITATION_SCORER_MODEL", "gpt-4o")
    marker = getattr(snippet, "citation_marker", None) or getattr(snippet, "marker", None)
    if not isinstance(marker, str) or not marker.strip():
        marker = _fallback_citation_marker(snippet, citation_label)
    marker = marker.strip()

    system = score_and_answer_snippet_prompt()

    payload = {
        "question": question,
        "focus_term": focus_term,
        "importance_hint": importance,
        "marker": marker,
        "page": getattr(snippet, "page", None),
        "why": getattr(snippet, "why", ""),
        "snippet_text": getattr(snippet, "text", ""),
        "section": getattr(snippet, "section_path", ""),
    }

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
    except Exception:
        log.error("Merged score+answer LLM call failed for snippet %s", marker, exc_info=True)
        data = {}

    # --- score fields ---
    context = str(data.get("context") or "").strip()
    if not context:
        text_preview = " ".join(str(getattr(snippet, "text", "") or "").split())[:320]
        context = text_preview

    relevance = _clamp_0_1(data.get("relevance"), 0.0)
    directness = _clamp_0_1(data.get("directness"), relevance)
    blended = (0.6 * relevance) + (0.4 * directness)
    model_score = _clamp_0_1(data.get("score"), blended)
    weighted = _clamp_0_1(model_score * importance, model_score)

    # --- answer field ---
    answer_raw = data.get("answer")
    answer_str = str(answer_raw).strip() if answer_raw is not None else ""
    if not answer_str:
        answer_str = "Not Relevant"

    return {
        "marker": marker,
        "page": getattr(snippet, "page", None),
        "snippet_id": getattr(snippet, "id", ""),
        "concept_term": focus_term,
        "importance": importance,
        "relevance": relevance,
        "directness": directness,
        "base_score": model_score,
        "score": weighted,
        "context": context,
        "why": getattr(snippet, "why", ""),
        "answer": answer_str,
    }


def extract_keywords(question: str, subject: str | None = None) -> str:
    """Summarize the governing subject principles emphasized by the user prompt."""

    client = _client()
    subject = subject or get_subject_name()
    system = extract_keywords_prompt(subject)
    payload = {
        "subject": subject,
        "question": question,
    }
    try:
        resp = client.chat.completions.create(
            model=os.getenv("PARSER_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            temperature=0,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception:
        log.error("Keyword extraction LLM call failed", exc_info=True)
        summary = ""

    if not summary:
        summary = question.strip()
    return summary


def filter_keywords_by_subject(
    context_summary: str, question: str | None = None, subject: str | None = None
) -> List[Dict[str, Any]] | None:
    """Produce standalone keyword terms using only the provided context summary and question."""

    if not (context_summary or question):
        return []

    client = _client()

    # Step 1: generate up to 20 candidate terms using only the question + context.
    gen_system = keyword_generation_prompt()
    gen_payload = {
        "question": question or "",
        "context_summary": context_summary or "",
        "max_terms": 20,
    }
    candidates: List[str] = []
    try:
        resp = client.chat.completions.create(
            model=os.getenv("PARSER_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": gen_system},
                {"role": "user", "content": json.dumps(gen_payload, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        raw_terms = data.get("terms") if isinstance(data, dict) else None
        if isinstance(raw_terms, list):
            for term in raw_terms:
                if not isinstance(term, str):
                    continue
                cleaned = term.strip().lower()
                if not cleaned or cleaned in candidates:
                    continue
                if any(ch in cleaned for ch in " -_/\\.'\""):
                    continue
                if len(cleaned.split()) > 1:
                    continue
                candidates.append(cleaned)
                if len(candidates) >= 20:
                    break
    except Exception:
        log.error("Keyword filtering LLM call failed (generate step)", exc_info=True)
        candidates = []

    if not candidates:
        return []

    # Step 2: score terms with subject knowledge and select the top 8.
    subject = subject or get_subject_name()
    score_system = keyword_scoring_prompt(subject)
    score_payload = {
        "subject": subject,
        "question": question or "",
        "context_summary": context_summary or "",
        "candidate_terms": candidates,
    }
    ranked: List[Dict[str, Any]] = []
    try:
        resp = client.chat.completions.create(
            model=os.getenv("PARSER_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": score_system},
                {"role": "user", "content": json.dumps(score_payload, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        ranked_entries = data.get("ranked") if isinstance(data, dict) else None
        if isinstance(ranked_entries, list):
            seen_terms: Set[str] = set()
            for entry in ranked_entries:
                if not isinstance(entry, dict):
                    continue
                term = entry.get("term")
                score = entry.get("score")
                if not isinstance(term, str):
                    continue
                cleaned = term.strip().lower()
                if not cleaned or cleaned.lower() in seen_terms:
                    continue
                if any(ch in cleaned for ch in " -_/\\.'\""):
                    continue
                if len(cleaned.split()) > 1:
                    continue
                try:
                    score_val = float(score)
                except (TypeError, ValueError):
                    continue
                seen_terms.add(cleaned.lower())
                ranked.append({"term": cleaned, "relevance": score_val})
    except Exception:
        log.error("Keyword filtering LLM call failed (rank step)", exc_info=True)
        ranked = []

    if not ranked:
        return []

    ranked.sort(key=lambda item: float(item.get("relevance", 0.0)), reverse=True)
    return ranked[:8]


def filter_general_terms(
    terms: List[Dict[str, Any]], subject: str | None = None
) -> List[Dict[str, Any]]:
    """Filter out overly general terms that could cause poor citation retrieval.
    
    This AI filter removes terms that are too broad or common in academic contexts
    to be useful for precise document retrieval, focusing on specific concepts
    and technical terms relevant to the subject. Defaults to a lenient mode to avoid
    over-pruning subject-relevant concepts.
    """
    
    if not terms:
        return []
    
    mode_env = os.getenv("GENERAL_FILTER_MODE", "lenient").lower()
    if mode_env in {"off", "none"}:
        return terms

    def _canonical(term: str) -> str:
        cleaned = (term or "").strip().lower()
        cleaned = re.sub(r"'s\b", "", cleaned)
        cleaned = cleaned.replace("'", "")
        cleaned = re.sub(r"[.,;:!?]+$", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _semantic_partition(
        normalized_terms: List[tuple[str, float]]
    ) -> tuple[Set[str], Set[str]]:
        """Decide which compounds/singles to keep, operating on normalized terms only."""

        generic_tails = {"equation", "principle", "law", "theorem", "formula", "effect", "effects"}

        candidate_compounds: Set[str] = set()
        candidate_singles: Set[str] = set()
        weight_map: Dict[str, float] = {}
        for term, weight in normalized_terms:
            weight_map[term] = max(weight_map.get(term, 0.0), weight)
            if " " in term:
                candidate_compounds.add(term)
            else:
                candidate_singles.add(term)

        preserved_compounds: Set[str] = set()
        preserved_singles: Set[str] = set()

        # Keep compounds as-is; optionally reduce trivial repetition like "bernoulli bernoulli"
        for comp in candidate_compounds:
            tokens = [t for t in comp.split(" ") if t]
            if not tokens:
                continue
            if len(tokens) == 2 and tokens[1] in generic_tails:
                head = tokens[0]
                head_weight = weight_map.get(head, 0.0)
                comp_weight = weight_map.get(comp, 0.0)
                weight_map[head] = max(head_weight, comp_weight)
                preserved_singles.add(head)
                continue
            # If all tokens identical, reduce to single token
            if len(set(tokens)) == 1:
                preserved_singles.add(tokens[0])
                continue
            preserved_compounds.add(comp)

        # Singles: keep those with reasonable length/letter content; drop weak-only components
        for single in candidate_singles:
            letters = re.findall(r"[a-z]", single)
            if len(letters) < 2:
                continue
            if len(single) <= 2:
                # Only keep if this short token never stands alone in compounds
                in_comp = any(f" {single} " in f" {c} " for c in preserved_compounds)
                if in_comp:
                    continue
            preserved_singles.add(single)

        # Remove singles that appear only as generic heads/tails of compounds and are very short
        trimmed_singles: Set[str] = set()
        for single in preserved_singles:
            if len(single) <= 3 and any(
                single in comp.split(" ") for comp in preserved_compounds
            ):
                continue
            trimmed_singles.add(single)

        return preserved_compounds, trimmed_singles

    client = _client()
    subject_name = subject or get_subject_name()
    
    # Extract just the term strings for analysis
    term_strings: List[str] = []
    term_map: Dict[str, Dict[str, Any]] = {}
    normalized: List[tuple[str, float]] = []
    for entry in terms:
        if isinstance(entry, dict):
            term = entry.get("term", "")
            if term:
                term_strings.append(term)
                term_map[term] = entry
                try:
                    rel = float(entry.get("relevance", 1.0))
                except Exception:
                    log.debug("Relevance float conversion failed, defaulting to 1.0")
                    rel = 1.0
                canon = _canonical(term)
                if canon:
                    normalized.append((canon, rel))
        elif isinstance(entry, str):
            term_strings.append(entry)
            term_map[entry] = {"term": entry, "relevance": 1.0}
            canon = _canonical(entry)
            if canon:
                normalized.append((canon, 1.0))
    
    if not term_strings:
        return []
    if not normalized:
        normalized = [(t, 1.0) for t in term_strings if t]
    
    system = general_term_filter_prompt(subject_name)
    
    payload = {
        "subject": subject_name,
        "terms": term_strings,
        "instruction": "Filter out general terms, keep specific technical concepts"
    }
    
    try:
        resp = client.chat.completions.create(
            model=os.getenv("PARSER_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        
        filtered_term_strings = data.get("filtered_terms", [])
        if not isinstance(filtered_term_strings, list):
            # Fallback to original terms if filtering fails
            return terms

        # Normalize LLM output
        filtered_norm = [_canonical(t) for t in filtered_term_strings if isinstance(t, str)]
        # Combine normalized originals to ensure we operate on canonical forms
        llm_normalized = [(t, 1.0) for t in filtered_norm if t]
        combined = normalized + llm_normalized

        preserved_compounds, preserved_singles = _semantic_partition(combined)

        final_terms: Set[str] = set()
        final_terms.update(preserved_singles)
        final_terms.update(preserved_compounds)

        # Build final list with relevance (use max seen relevance)
        relevance_lookup: Dict[str, float] = {}
        for term, weight in combined:
            relevance_lookup[term] = max(relevance_lookup.get(term, 0.0), weight)

        filtered_terms: List[Dict[str, Any]] = []
        for term in sorted(final_terms):
            rel = relevance_lookup.get(term, 1.0)
            filtered_terms.append({"term": term, "relevance": rel})

        if len(filtered_terms) < 1:
            return terms

        if WIRE:
            print(f"[Main AI -> General Filter] raw_terms={term_strings}", flush=True)
            print(f"[Main AI -> General Filter] normalized_terms={sorted({t for t, _ in combined})}", flush=True)
            print(f"[Main AI -> General Filter] final_terms={[t['term'] for t in filtered_terms]}", flush=True)

        return filtered_terms

    except Exception:
        log.error("General term filter LLM call failed", exc_info=True)
        # If the filter fails, return original terms to avoid breaking the pipeline
        return terms


def propose_synonyms(
    terms: List[str], context_hint: Dict[str, Any] | None = None, subject: str | None = None
) -> Dict[str, List[str]]:
    """Ask the LLM to generate 1–2 plausible synonyms per term."""

    if not terms:
        return {}

    client = _client()
    subject = subject or get_subject_name()
    system = synonyms_prompt(subject)
    hint = context_hint or {}
    try:
        resp = client.chat.completions.create(
            model=os.getenv("PARSER_MODEL", "gpt-4o"),
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
        log.error("Synonym proposal LLM call failed", exc_info=True)
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


def extract_and_filter_keywords(
    question: str, subject: str | None = None
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract, score, rank, and filter keywords in a single LLM call.

    Merges the work of ``extract_keywords``, ``filter_keywords_by_subject``
    (both steps), and ``filter_general_terms`` into one API round-trip.

    Returns ``(context_summary, ranked_filtered_terms)`` where each term entry
    is ``{"term": str, "relevance": float}``.
    """
    client = _client()
    subject = subject or get_subject_name()

    system = concept_extraction_prompt(subject)

    payload = {"subject": subject, "question": question}

    try:
        resp = client.chat.completions.create(
            # Default stays gpt-4o: measured 1.3-2.1s vs 3.0-4.2s on gpt-4o-mini
            # (mini emits more terms at lower tokens/s). KEYWORD_MODEL is the
            # override knob if pricing/latency tradeoffs change.
            model=os.getenv("KEYWORD_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
    except Exception:
        log.error("Merged keyword extraction+filtering LLM call failed", exc_info=True)
        return question.strip(), []

    context_summary = str(data.get("context_summary", "")).strip()
    if not context_summary:
        context_summary = question.strip()

    ranked_terms: List[Dict[str, Any]] = []
    raw_ranked = data.get("ranked_terms")
    if isinstance(raw_ranked, list):
        seen: Set[str] = set()
        for entry in raw_ranked:
            if not isinstance(entry, dict):
                continue
            term = entry.get("term")
            score = entry.get("relevance")
            if not isinstance(term, str):
                continue
            cleaned = " ".join(term.strip().lower().split())
            if not cleaned or cleaned in seen:
                continue
            if any(ch in cleaned for ch in "/\\.\""):
                continue
            if len(cleaned.split()) > 5:
                continue
            try:
                score_val = float(score)
            except (TypeError, ValueError):
                continue
            seen.add(cleaned)
            ranked_terms.append({"term": cleaned, "relevance": score_val})
            if len(ranked_terms) >= 8:
                break

    ranked_terms.sort(key=lambda x: float(x.get("relevance", 0.0)), reverse=True)
    return context_summary, ranked_terms


def parse_question(user_query: str, subject: str | None = None) -> ParsedTask:
    """Use a lightweight model to parse the raw user query into a ``ParsedTask``."""

    client = _client()
    subject = subject or get_subject_name()
    system = parse_question_prompt(subject)
    model = os.getenv("PARSER_MODEL", "gpt-4o")
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


def _is_reasoning_model(model: str) -> bool:
    """True if the model takes a `reasoning` param (gpt-5 family)."""
    gpt5_allow = {"gpt-5", "gpt-5-chat-latest", "gpt-5-mini"}
    return model.startswith("gpt-5") or model in gpt5_allow


def _build_solution_from_data(data: Dict[str, Any]) -> ProposedSolution:
    """Map a solver JSON dict to a ProposedSolution. Pure; no I/O.

    Shared by the blocking solve_with_bundle and the streaming
    solve_with_bundle_stream so both produce identical solutions.
    """
    if data.get("not_relevant", False):
        return ProposedSolution(
            steps="This question is not relevant to the course scope.",
            final_answers={},
            equations_used=[],
            assumptions=[],
            code=None,
            code_output=None,
            code_hash=None,
            vars_created=[],
        )

    raw_steps = data.get("steps", "")
    if isinstance(raw_steps, list):
        # Join list elements as paragraphs instead of JSON-serialising them.
        raw_steps = "\n\n".join(
            elem if isinstance(elem, str) else str(elem) for elem in raw_steps
        )
    elif not isinstance(raw_steps, str):
        raw_steps = str(raw_steps)

    # Enforce conceptual-only mode regardless of model output.
    final_answers_output: Dict[str, Any] = {}
    equations_used = data.get("equations_used", [])
    assumptions = data.get("assumptions", [])

    # Ensure structured fields are in expected shapes.
    if not isinstance(equations_used, list):
        equations_used = [equations_used] if equations_used else []
    if not isinstance(assumptions, list):
        assumptions = [assumptions] if assumptions else []

    return ProposedSolution(
        steps=raw_steps,
        final_answers=final_answers_output,
        equations_used=equations_used,
        assumptions=assumptions,
        code=None,
        code_output=None,
        code_hash=None,
        vars_created=[],
    )


def _prepare_solve_prompt(
    parsed_task: ParsedTask, bundle: ResearchBundle, hint: str | None = None,
    subject: str | None = None,
) -> Tuple[str, str, str]:
    """Run snippet scoring and build the solver prompt.

    Returns (system, user_base, model). All side effects (miniresponse files,
    provenance 'citation_rankings', debug dumps) are preserved exactly as in the
    original solve_with_bundle. Shared by the blocking and streaming solve paths.
    """
    question_text = ""
    try:
        question_text = getattr(bundle.metadata, "question", "") or getattr(
            bundle.metadata, "original_query", ""
        )
    except Exception:
        log.warning("Failed to extract question from bundle metadata")
        question_text = ""

    q = question_text or parsed_task.problem_type
    scorer_model = os.getenv("CITATION_SCORER_MODEL", "gpt-4o")

    # Build per-snippet args before submitting to the pool
    snippet_args: List[Tuple[Any, float, str]] = []
    for sn in bundle.snippets:
        focus_term = _pick_concept_term(sn)
        importance = _importance_from_snippet(sn)
        snippet_args.append((sn, importance, focus_term))

    # Run merged score+answer in parallel (Phase 2+3)
    max_workers = _citation_pool_size(len(snippet_args))
    combined_results: List[Dict[str, Any]] = []
    _t_score = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _score_and_answer_snippet,
                q, sn, importance, focus_term,
                model=scorer_model,
            )
            for sn, importance, focus_term in snippet_args
        ]
        for future in futures:
            try:
                combined_results.append(future.result())
            except Exception:
                log.error("Parallel snippet processing failed", exc_info=True)
                combined_results.append({
                    "marker": "?",
                    "page": None,
                    "snippet_id": "",
                    "concept_term": "",
                    "importance": 0.0,
                    "relevance": 0.0,
                    "directness": 0.0,
                    "base_score": 0.0,
                    "score": 0.0,
                    "context": "",
                    "why": "",
                    "answer": "Not Relevant",
                })

    log.info(
        "[timing] snippet_scoring=%.2fs n=%d",
        time.perf_counter() - _t_score, len(snippet_args),
    )

    # Split into citation_analyses (score fields) and per_citation_answers (answer fields)
    citation_analyses: List[Dict[str, Any]] = []
    per_citation_answers: List[Dict[str, Any]] = []
    for r in combined_results:
        citation_analyses.append({
            "marker": r.get("marker", ""),
            "page": r.get("page"),
            "concept_term": r.get("concept_term", ""),
            "importance": r.get("importance", 0.0),
            "relevance": r.get("relevance", 0.0),
            "directness": r.get("directness", 0.0),
            "base_score": r.get("base_score", 0.0),
            "score": r.get("score", 0.0),
            "context": r.get("context", ""),
            "why": r.get("why", ""),
            "snippet_id": r.get("snippet_id", ""),
        })
        per_citation_answers.append({
            "marker": r.get("marker", ""),
            "page": r.get("page"),
            "snippet_id": r.get("snippet_id", ""),
            "score": r.get("score"),
            "answer": r.get("answer", "Not Relevant"),
        })

    citation_analyses.sort(
        key=lambda row: (
            -float(row.get("score", 0.0) or 0.0),
            -float(row.get("importance", 0.0) or 0.0),
            -float(row.get("relevance", 0.0) or 0.0),
            str(row.get("marker", "")),
        )
    )
    try:
        bundle.provenance.setdefault("citation_rankings", citation_analyses)
    except Exception:
        log.warning("Failed to set citation_rankings in provenance")

    per_citation_answers.sort(
        key=lambda row: (-(row.get("score") or 0.0), row.get("marker", ""))
    )
    _write_miniresponses(per_citation_answers, q)

    # --- Filter: drop snippets scored below threshold ---
    score_floor = float(os.getenv("CITATION_SCORE_FLOOR", "0.3"))
    scored_snippets: List[Dict[str, Any]] = []
    for i, r in enumerate(combined_results):
        score_val = float(r.get("score", 0.0) or 0.0)
        if score_val < score_floor:
            continue
        sn = bundle.snippets[i]
        scored_snippets.append({
            "marker": r.get("marker", ""),
            "page": r.get("page"),
            "score": score_val,
            "section": getattr(sn, "section_path", ""),
            "source_text": getattr(sn, "text", ""),
        })
    scored_snippets.sort(key=lambda x: -x["score"])

    system = tutor_prompt()
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
        try:
            proof_json = json.dumps(proof_bundle, ensure_ascii=False)
        except TypeError:
            log.debug("Proof bundle not JSON-serializable")
            proof_json = None
    # Build source excerpts payload (original text, not mini-summaries)
    source_excerpts: List[Dict[str, Any]] = []
    for entry in scored_snippets:
        source_excerpts.append({
            "marker": entry["marker"],
            "page": entry["page"],
            "relevance_score": round(entry["score"], 2),
            "section": entry["section"],
            "text": entry["source_text"],
        })
    excerpts_json = json.dumps(source_excerpts, ensure_ascii=False)

    payload_lines = [
        f"Task: {json.dumps(asdict(parsed_task), ensure_ascii=False)}",
        f"Question: {question_text or parsed_task.problem_type}",
        f"SourceExcerpts (sorted high->low relevance): {excerpts_json}",
    ]
    if proof_json:
        payload_lines.append(f"FullProofBundle: {proof_json}")

    # Inject RelevanceNote for partially on-topic questions
    prov = getattr(bundle, "provenance", {}) or {}
    if prov.get("relevance_level") == "partial":
        on_topic = prov.get("on_topic_portion", "")
        off_topic = prov.get("off_topic_portion", "")
        if on_topic or off_topic:
            payload_lines.append(
                f"RelevanceNote: The student's question is partially on-topic. "
                f"The on-topic portion is: '{on_topic}'. "
                f"The off-topic portion is: '{off_topic}'. "
                f"Answer the on-topic part thoroughly with citations, then add a brief "
                f"redirect note acknowledging the off-topic part is outside course scope."
            )

    payload_lines.append("Return JSON with keys: not_relevant, steps, final_answers, equations_used, assumptions.")
    payload_lines.append(
        "- not_relevant: boolean — true if the question is outside the course scope, false otherwise."
    )
    payload_lines.append(
        "- steps: a SINGLE Markdown-formatted string (NOT an array). Follow the tutor system prompt "
        "to determine which sections to include: for new questions use all three sections "
        "(## Answer, ## Key Takeaway, ## Check Your Understanding); for CYU responses follow "
        "the CYU RESPONSE RULES (brief affirmation if correct, corrective feedback + new CYU if incorrect). "
        "Use LaTeX math ($...$ inline, $$...$$ display). Follow the LENGTH RULES in the system prompt "
        "to size the response to the question type. No long paragraphs."
    )
    payload_lines.append("- final_answers: MUST be an empty object {} because you are not computing results.")
    payload_lines.append(
        "- equations_used: list of symbolic equations (variables/constants only, no numbers substituted)."
    )
    payload_lines.append("- assumptions: list of textual assumptions or conditions.")
    payload_lines.append("Do NOT include any code field; do NOT run or describe executable code.")
    user_base = "\n".join(payload_lines)
    if hint:
        user_base += f"\nHint: {hint}"
    model = os.getenv("MAIN_MODEL", "gpt-5")

    def _maybe_debug_dump(
        system_prompt: str,
        user_payload: str,
        bundle: ResearchBundle,
        proof_bundle: Optional[Dict[str, Any]] = None,
        citation_contexts: Optional[List[Dict[str, Any]]] = None,
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

        if citation_contexts:
            try:
                with open("debug/main_ai_citation_rankings.json", "w", encoding="utf-8") as fh:
                    json.dump(citation_contexts, fh, indent=2, ensure_ascii=False)
            except Exception:
                log.debug("Failed to write citation rankings debug file")
            lines.append("citation_ranking (top to bottom):")
            for ctx in citation_contexts[:25]:
                lines.append(
                    f"- score={ctx.get('score')} imp={ctx.get('importance')} marker={ctx.get('marker')} term={ctx.get('concept_term')} why={ctx.get('why')}"
                )

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

    _maybe_debug_dump(system, user_base, bundle, proof_bundle, citation_analyses)

    return system, user_base, model


def solve_with_bundle(
    parsed_task: ParsedTask, bundle: ResearchBundle, hint: str | None = None,
    subject: str | None = None,
) -> ProposedSolution:
    """Solve the parsed task using only information from the provided bundle."""
    client = _client()
    system, user_base, model = _prepare_solve_prompt(parsed_task, bundle, hint, subject)

    def _chat(msgs: List[dict]) -> dict:
        kwargs = {
            "model": model,
            "messages": msgs,
            "response_format": {"type": "json_object"},
        }
        if _is_reasoning_model(model):
            kwargs["reasoning_effort"] = os.getenv("MAIN_REASONING_EFFORT", "high")
        else:
            kwargs["temperature"] = 0
        kwargs["prompt_cache_key"] = os.getenv(
            "PROMPT_CACHE_KEY", f"aita-solver:{model}"
        )
        service_tier = (os.getenv("OPENAI_SERVICE_TIER") or "").strip()
        if service_tier:
            kwargs["service_tier"] = service_tier
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        return json.loads(content)

    _t_solve = time.perf_counter()
    data = _chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user_base},
    ])
    log.info("[timing] solve=%.2fs model=%s", time.perf_counter() - _t_solve, model)

    return _build_solution_from_data(data)


def solve_with_bundle_stream(
    parsed_task: ParsedTask, bundle: ResearchBundle, hint: str | None = None,
    subject: str | None = None,
) -> "Iterator[Tuple[str, Any]]":
    """Generator: yields ("reasoning", str) summary deltas during the think
    phase, ("token", str) decoded answer deltas, then ("solution",
    ProposedSolution). Uses the Responses API so reasoning summaries stream.

    Dispatches on event.type and ignores unknown events, so if the account does
    not emit reasoning summaries it degrades to answer-only streaming.
    """
    from ai.streaming import JsonStringFieldStreamer

    client = _client()
    system, user_base, model = _prepare_solve_prompt(parsed_task, bundle, hint, subject)

    kwargs: Dict[str, Any] = {
        "model": model,
        "instructions": system,
        "input": user_base,
        "text": {"format": {"type": "json_object"}},
        "stream": True,
    }
    if _is_reasoning_model(model):
        kwargs["reasoning"] = {
            "effort": os.getenv("MAIN_REASONING_EFFORT", "high"),
            "summary": "auto",
        }
    else:
        kwargs["temperature"] = 0

    # Prompt-cache routing: tutor_prompt() instructions are the static prefix;
    # a stable cache key routes repeat requests to the same cache (per-key
    # throughput limit ~15 RPM — fine at current traffic).
    kwargs["prompt_cache_key"] = os.getenv(
        "PROMPT_CACHE_KEY", f"aita-solver:{model}"
    )
    service_tier = (os.getenv("OPENAI_SERVICE_TIER") or "").strip()
    if service_tier:
        kwargs["service_tier"] = service_tier
    verbosity = (os.getenv("MAIN_VERBOSITY") or "").strip()
    if verbosity and _is_reasoning_model(model):
        kwargs["text"]["verbosity"] = verbosity

    streamer = JsonStringFieldStreamer(field="steps")
    json_buf: List[str] = []

    seen_types: set[str] = set()
    _t_solve = time.perf_counter()
    for event in client.responses.create(**kwargs):
        etype = getattr(event, "type", "")
        seen_types.add(etype)
        if etype == "response.reasoning_summary_text.delta":
            delta = getattr(event, "delta", "") or ""
            if delta:
                yield ("reasoning", delta)
        elif etype == "response.output_text.delta":
            delta = getattr(event, "delta", "") or ""
            if not delta:
                continue
            json_buf.append(delta)
            text = streamer.feed(delta)
            if text:
                yield ("token", text)

    log.info("[stream] response event types seen: %s", sorted(seen_types))
    log.info("[timing] solve_stream=%.2fs model=%s", time.perf_counter() - _t_solve, model)

    full = "".join(json_buf)
    try:
        data = json.loads(full)
    except Exception:
        log.error("Streaming solve produced unparseable JSON; empty solution")
        data = {}
    yield ("solution", _build_solution_from_data(data))


def format_answer(
    solution: ProposedSolution,
    bundle: ResearchBundle,
    *,
    include_background: bool = True,
    citation_label: str | None = None,
    subject: str | None = None,
) -> FinalAnswer:
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
                        log.debug("Element JSON serialization failed in _to_str")
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
    if text_str.strip() == "This question is not relevant to the course scope.":
        return FinalAnswer(text="This question is not relevant to the course scope.", citations=[])

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
                marker = _fallback_citation_marker(sn, citation_label=citation_label)
            cleaned = marker.strip()
            if cleaned and cleaned not in allowed_seen:
                allowed_markers.append(cleaned)
                allowed_seen.add(cleaned)
    if not allowed_markers:
        fallback = f"[{citation_label or get_citation_label()}, p. ?]"
        allowed_markers.append(fallback)
        allowed_seen.add(fallback)
    allowed_set: set[str] = set(allowed_markers)

    snippet_infos: List[tuple[str, str, str]] = []
    info_seen: set[str] = set()
    for sn in bundle.snippets:
        marker = getattr(sn, "citation_marker", None) or getattr(sn, "marker", None)
        if not isinstance(marker, str) or not marker.strip():
            marker = _fallback_citation_marker(sn, citation_label=citation_label)
        cleaned = marker.strip()
        if cleaned and cleaned not in allowed_set:
            allowed_markers.append(cleaned)
            allowed_set.add(cleaned)
        if cleaned and cleaned not in info_seen:
            info_seen.add(cleaned)
            reason = getattr(sn, "why", "") or "context"
            snippet_text = getattr(sn, "text", "")
            snippet_infos.append((cleaned, reason, snippet_text))

    if include_background and snippet_infos:
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
    if include_background and missing_terms:
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
        # Skip code blocks, display math, and markdown headings.
        if (
            stripped.startswith("```")
            or stripped.startswith("$$")
            or stripped.startswith("#")
        ):
            paragraphs[idx] = _clean_and_tag(para)
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

    return FinalAnswer(text=final_text, citations=used_markers)


def _write_proof_citations(
    bundle: ResearchBundle, allowed_markers: List[str], used_markers: List[str]
) -> None:
    """Persist a lightweight JSON payload for downstream proof review tooling."""

    try:
        question = getattr(bundle.metadata, "question", "")
    except Exception:
        log.warning("Failed to extract question for proof citations")
        question = ""
    dedup_allowed = []
    seen_allowed: set[str] = set()
    for marker in allowed_markers:
        if not isinstance(marker, str):
            continue
        cleaned = marker.strip()
        if cleaned and cleaned not in seen_allowed:
            seen_allowed.add(cleaned)
            dedup_allowed.append(cleaned)
    dedup_used = []
    seen_used: set[str] = set()
    for marker in used_markers:
        if not isinstance(marker, str):
            continue
        cleaned = marker.strip()
        if cleaned and cleaned not in seen_used:
            seen_used.add(cleaned)
            dedup_used.append(cleaned)
    snippet_rows: List[Dict[str, Any]] = []
    for sn in bundle.snippets:
        snippet_rows.append(
            {
                "id": getattr(sn, "id", ""),
                "type": getattr(sn, "type", ""),
                "page": getattr(sn, "page", None),
                "section_path": getattr(sn, "section_path", ""),
                "text": getattr(sn, "text", ""),
                "figure_id": getattr(sn, "figure_id", None),
                "why": getattr(sn, "why", ""),
                "terms": list(getattr(sn, "concept_terms", []) or []),
            }
        )
    payload = {
        "question": question,
        "allowed_markers": dedup_allowed,
        "used_markers": dedup_used,
        "used_ids": list(getattr(bundle, "used_ids", []) or []),
        "snippets": snippet_rows,
    }
    try:
        Path("proofhoot.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        # Best-effort diagnostic file; ignore failures so core execution continues.
        log.warning("Failed to write proofhoot.json diagnostic file")


def _write_citations_file(
    bundle: ResearchBundle, allowed_markers: List[str], used_markers: List[str]
) -> None:
    """Write a detailed citations snapshot for tooling or manual inspection."""

    equal_map = getattr(bundle, "marker_equal_map", {}) or {}
    marker_terms: Dict[str, Set[str]] = {}
    for sn in bundle.snippets:
        marker = getattr(sn, "citation_marker", None) or getattr(sn, "marker", None)
        if not isinstance(marker, str):
            continue
        cleaned_marker = marker.strip()
        if not cleaned_marker:
            continue
        terms = getattr(sn, "concept_terms", None)
        if not terms:
            continue
        term_set = marker_terms.setdefault(cleaned_marker, set())
        for term in terms:
            if isinstance(term, str):
                stripped = term.strip()
                if stripped:
                    term_set.add(stripped)

    marker_rows: List[Dict[str, Any]] = []
    if isinstance(equal_map, dict) and equal_map:
        for marker, score in sorted(equal_map.items(), key=lambda item: item[1], reverse=True):
            if not isinstance(marker, str):
                continue
            cleaned = marker.strip()
            if not cleaned:
                continue
            try:
                row: Dict[str, Any] = {"marker": cleaned, "equal": float(score)}
            except Exception:
                log.debug("Score float conversion failed for marker %s", cleaned)
                row = {"marker": cleaned}
            terms_list = sorted(marker_terms.get(cleaned, []))
            if terms_list:
                row["terms"] = terms_list
            marker_rows.append(row)
    else:
        seen: set[str] = set()
        for marker in allowed_markers:
            if not isinstance(marker, str):
                continue
            cleaned = marker.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                row = {"marker": cleaned}
                terms_list = sorted(marker_terms.get(cleaned, []))
                if terms_list:
                    row["terms"] = terms_list
                marker_rows.append(row)
    dedup_used = []
    seen_used: set[str] = set()
    for marker in used_markers:
        if not isinstance(marker, str):
            continue
        cleaned = marker.strip()
        if cleaned and cleaned not in seen_used:
            seen_used.add(cleaned)
            dedup_used.append(cleaned)
    snippets_summary: List[Dict[str, Any]] = []
    for sn in bundle.snippets:
        record: Dict[str, Any] = {
            "id": getattr(sn, "id", ""),
            "marker": getattr(sn, "citation_marker", ""),
            "page": getattr(sn, "page", None),
            "why": getattr(sn, "why", ""),
            "source_path": getattr(sn, "source_path", ""),
            # Include document title/alias so downstream tools can distinguish
            # citations coming from different embedded sources (textbook, slides, notes, etc.).
            "doc_title": getattr(sn, "doc_title", None),
            "doc_short": getattr(sn, "doc_short", ""),
        }
        terms = getattr(sn, "concept_terms", None)
        if terms:
            record["terms"] = list(terms)
        final_score = getattr(sn, "final_score", None)
        if isinstance(final_score, dict) and final_score:
            record["final_score"] = final_score
        snippets_summary.append(record)
    metadata = getattr(bundle, "metadata", None)
    doc_sets: List[str] = []
    iteration_trace: List[Dict[str, Any]] = []
    not_found_terms: List[str] = []
    attempted_terms: List[str] = []
    if metadata is not None:
        doc_sets = list(getattr(metadata, "doc_sets", []) or [])
        iteration_trace = list(getattr(metadata, "iteration_trace", []) or [])
        not_found_terms = list(getattr(metadata, "not_found_terms", []) or [])
        attempted_terms = list(getattr(metadata, "attempted_terms", []) or [])
    bundle_not_found = list(getattr(bundle, "not_found_terms", []) or [])
    bundle_attempted = list(getattr(bundle, "attempted_terms", []) or [])
    if bundle_not_found:
        for term in bundle_not_found:
            if term not in not_found_terms:
                not_found_terms.append(term)
    if bundle_attempted:
        for term in bundle_attempted:
            if term not in attempted_terms:
                attempted_terms.append(term)
    question = ""
    if metadata is not None:
        question = getattr(metadata, "question", "") or ""
    payload = {
        "question": question,
        "doc_sets": doc_sets,
        "allowed_markers": marker_rows,
        "used_markers": dedup_used,
        "used_ids": list(getattr(bundle, "used_ids", []) or []),
        "iteration_trace": iteration_trace,
        "not_found_terms": not_found_terms,
        "attempted_terms": attempted_terms,
        "snippets": snippets_summary,
    }
    try:
        outdir = get_runtime_dir() / "debug"
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / "citations.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        log.warning("Failed to write citations.json debug file")


def _write_miniresponses(
    per_citation_answers: List[Dict[str, Any]], question: str
) -> None:
    """Persist per-citation mini responses for inspection."""

    payload = {
        "question": question,
        "per_citation_answers": per_citation_answers,
    }
    try:
        outdir = get_runtime_dir() / "debug"
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / "miniresponses.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        log.warning("Failed to write miniresponses.json debug file")


__all__ = [
    "parse_question",
    "solve_with_bundle",
    "solve_with_bundle_stream",
    "format_answer",
    "normalize_query",
    "is_question_subject_relevant",
    "extract_keywords",
    "filter_general_terms",
    "propose_synonyms",
    "_write_miniresponses",
]
