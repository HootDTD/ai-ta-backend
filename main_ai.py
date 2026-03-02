from __future__ import annotations

"""Wrapper functions for the user-facing agent."""

import json
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

from openai import OpenAI

from .config import get_subject_name, get_citation_label, get_runtime_dir
from .contracts import ParsedTask, ProposedSolution, FinalAnswer, ResearchBundle
from .solver import run_python


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


def is_question_subject_relevant(question: str, subject: str | None = None) -> bool:
    """Use the LLM to decide whether the question belongs to the active subject."""

    cleaned = (question or "").strip()
    if not cleaned:
        return False

    client = _client()
    subject = subject or get_subject_name()
    system = (
        f"You are a guard for the {subject} course materials. "
        "Decide if the student's question requires knowledge from this subject. "
        "Return JSON with keys 'relevant' (bool) and 'reason' (string). "
        "Mark relevant=false if the question is primarily about another discipline or general trivia."
    )
    payload = {
        "subject": subject,
        "question": cleaned,
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
        relevant = data.get("relevant")
        if isinstance(relevant, bool):
            return relevant
        if isinstance(relevant, str):
            lowered = relevant.strip().lower()
            if lowered in {"true", "yes", "y"}:
                return True
            if lowered in {"false", "no", "n"}:
                return False
    except Exception:
        log.error("Subject relevance guard failed", exc_info=True)
    # Fail open so that legitimate questions are not blocked if the guard fails.
    return True


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


def _score_citation_snippets(
    question: str, bundle: ResearchBundle, model: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Run per-citation mini-assessments to score snippets against the prompt."""

    client = _client()
    scorer_model = model or os.getenv("CITATION_SCORER_MODEL", os.getenv("PARSER_MODEL", "gpt-4o"))
    system = (
        "You are a strict citation assessor. "
        "Given the student's question, a focus term tied to the citation, the snippet text, and its marker, "
        "write 1-2 sentences explaining only how the snippet connects to the question. "
        "Score 'relevance' (0-1) for how well the snippet addresses the question and 'directness' (0-1) for how on-point it is. "
        "Blend these into 'score' (0-1) and allow the provided importance_hint to slightly boost or reduce the score when appropriate. "
        "Use ONLY the provided snippet text; do not add facts or context not present in the snippet. "
        "Return JSON with keys: context (string), relevance (0-1), directness (0-1), score (0-1)."
    )

    analyses: List[Dict[str, Any]] = []
    for sn in bundle.snippets:
        marker = getattr(sn, "citation_marker", None) or getattr(sn, "marker", None)
        if not isinstance(marker, str) or not marker.strip():
            marker = _fallback_citation_marker(sn)
        marker_clean = marker.strip()
        focus_term = _pick_concept_term(sn)
        importance = _importance_from_snippet(sn)
        payload = {
            "question": question,
            "focus_term": focus_term,
            "importance_hint": importance,
            "marker": marker_clean,
            "page": getattr(sn, "page", None),
            "why": getattr(sn, "why", ""),
            "snippet_text": getattr(sn, "text", ""),
            "section": getattr(sn, "section_path", ""),
        }

        try:
            resp = client.chat.completions.create(
                model=scorer_model,
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
            log.error("Citation scoring LLM call failed", exc_info=True)
            data = {}

        context = str(data.get("context") or data.get("summary") or "").strip()
        if not context:
            text_preview = " ".join(str(getattr(sn, "text", "") or "").split())[:320]
            context = text_preview

        relevance = _clamp_0_1(data.get("relevance"), 0.0)
        directness = _clamp_0_1(data.get("directness"), relevance)
        blended = (0.6 * relevance) + (0.4 * directness)
        model_score = _clamp_0_1(data.get("score"), blended)
        weighted = _clamp_0_1(model_score * importance, model_score)

        analyses.append(
            {
                "marker": marker_clean,
                "page": getattr(sn, "page", None),
                "concept_term": focus_term,
                "importance": importance,
                "relevance": relevance,
                "directness": directness,
                "base_score": model_score,
                "score": weighted,
                "context": context,
                "why": getattr(sn, "why", ""),
                "snippet_id": getattr(sn, "id", ""),
            }
        )

    analyses.sort(
        key=lambda row: (
            -float(row.get("score", 0.0) or 0.0),
            -float(row.get("importance", 0.0) or 0.0),
            -float(row.get("relevance", 0.0) or 0.0),
            str(row.get("marker", "")),
        )
    )
    return analyses


def _answer_single_snippet(
    question: str, snippet: Any, score: float | None = None
) -> Dict[str, Any]:
    """Have a per-citation agent extract all question-relevant information from a single snippet."""

    client = _client()
    model = os.getenv("CITATION_ANSWER_MODEL", os.getenv("PARSER_MODEL", "gpt-4o"))
    marker = getattr(snippet, "citation_marker", None) or getattr(snippet, "marker", None)
    if not isinstance(marker, str) or not marker.strip():
        marker = _fallback_citation_marker(snippet)
    marker = marker.strip()
    system = (
        "You are one member of a team of citation specialists. You are responsible for a single snippet of text taken "
        "from an approved course resource. Use ONLY the provided snippet_text as your source of information.\n\n"
        "Your goal is not to fully answer the user's question on your own, but to extract every piece of information in "
        "this snippet that could help another assistant answer the question when combined with other snippets. This "
        "includes definitions, qualitative descriptions, equations or relationships, assumptions, boundary conditions, "
        "parameter meanings, constraints, or any contextual clues that narrow down what is going on.\n\n"
        "Rules:\n"
        "- Base everything strictly on snippet_text; do NOT add outside knowledge or speculate beyond what the text clearly implies.\n"
        "- If the snippet is even partially related to the question (shares variables, concepts, or scenario features), "
        "summarize those relevant pieces in your own words.\n"
        "- It is acceptable if your answer is incomplete; other snippets will fill in gaps.\n"
        "- Only return exactly 'Not Relevant' if the snippet is clearly about an unrelated topic with no overlap in "
        "concepts, variables, or context with the question.\n\n"
        "Return JSON with a single key 'answer' containing your explanation as a string."
    )
    payload = {
        "question": question,
        "marker": marker,
        "page": getattr(snippet, "page", None),
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
        answer = data.get("answer")
    except Exception:
        log.error("Per-snippet answer LLM call failed", exc_info=True)
        answer = None

    answer_str = str(answer).strip() if answer is not None else ""
    if not answer_str:
        answer_str = "Not Relevant"

    return {
        "marker": marker,
        "page": getattr(snippet, "page", None),
        "snippet_id": getattr(snippet, "id", ""),
        "score": float(score) if score is not None else None,
        "answer": answer_str,
    }


def _score_and_answer_snippet(
    question: str,
    snippet: Any,
    importance: float,
    focus_term: str,
    citation_label: str | None = None,
    model: str | None = None,
) -> Dict[str, Any]:
    """Score relevance AND extract answer from a single snippet in one LLM call.

    Merges the work of ``_score_citation_snippets`` (per-snippet) and
    ``_answer_single_snippet`` so that only **one** API round-trip is needed
    per snippet instead of two.
    """
    client = _client()
    model = model or os.getenv(
        "CITATION_SCORER_MODEL", os.getenv("PARSER_MODEL", "gpt-4o")
    )
    marker = getattr(snippet, "citation_marker", None) or getattr(snippet, "marker", None)
    if not isinstance(marker, str) or not marker.strip():
        marker = _fallback_citation_marker(snippet, citation_label)
    marker = marker.strip()

    system = (
        "You are a citation specialist. Given a student's question and a single snippet "
        "from course materials, do TWO things:\n\n"
        "1. SCORE the snippet:\n"
        "   - relevance (0-1): how well the snippet addresses the question\n"
        "   - directness (0-1): how on-point the snippet is\n"
        "   - score (0-1): blended score; allow the provided importance_hint to slightly adjust\n"
        "   - context: 1-2 sentences explaining only how the snippet connects to the question\n\n"
        "   INTENT-AWARE SCORING — read the question carefully to determine the student's intent:\n"
        "   - If the question asks 'what is', 'define', 'explain', or 'why': the student wants "
        "conceptual understanding. Score HIGHEST for snippets that define, introduce, or explain "
        "the concept. Score LOWER for snippets that merely use the term in a worked example or "
        "unrelated derivation without explaining it.\n"
        "   - If the question asks 'how to', 'calculate', 'find', 'solve', or 'derive': the student "
        "wants a procedure or formula. Score HIGHEST for snippets with relevant equations, worked "
        "examples, or step-by-step methods.\n"
        "   - If the snippet comes from a section whose title directly names the topic being asked "
        "about (e.g. asking about 'boundary layer' and the section is 'Boundary Layer Theory'), "
        "this is strong evidence the snippet is authoritative — boost its score.\n"
        "   - A snippet that only mentions the term in passing (e.g. as a variable in an unrelated "
        "exercise) should score significantly lower than one that substantively addresses it.\n\n"
        "2. EXTRACT every piece of information in this snippet that could help answer "
        "the question. Include definitions, equations, relationships, assumptions, "
        "boundary conditions, parameter meanings, constraints, or contextual clues.\n"
        "   - Base everything strictly on snippet_text; do NOT add outside knowledge.\n"
        "   - Only return 'Not Relevant' for the answer field if the snippet is clearly "
        "about an unrelated topic with no overlap.\n\n"
        "Return JSON with keys: context (string), relevance (0-1), directness (0-1), "
        "score (0-1), answer (string)."
    )

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
    system = (
        f"You analyze {subject} textbook questions. Identify only the core principles or equations explicitly referenced in the prompt. "
        "List the topic names without elaborating or explaining them in detail. "
        "Respond with a single short sentence or comma-separated list naming the relevant topics."
    )
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
    gen_system = (
        "Generate candidate lookup terms using ONLY the student's raw question and the context summary. "
        "Do not add outside knowledge. "
        "Each candidate must be a single lowercase word (no spaces, hyphens, or punctuation). " \
        "If a necessary term needs multiple words to make sense, separate these two words with underscores. (only do this if the single word alternative could mean something else). "
        "Focus on including discrete concepts, principles, or keywords that directly relate to the question and context."
        "ALWAYS include individual terms from every topic mentioned in the context summary (e.g., if context contains 'Bernoulli's Principle', include 'bernoulli', 'principle')."
        "Avoid general terms, or overly broad concepts."
        "Return JSON {{\"terms\": [\"term1\", ...]}} with at most 20 short entries, each representing a discrete concept."
        "Be GENEROUS in proposing terms, as long as they are relevant to the question and context. Try to reach 20 terms if possible."
    )
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
    score_system = (
        f"You rank lookup terms for the subject \"{subject}\". "
        "Use the student's question, the concise context summary, and the list of candidate terms. "
        "Assign each term a UNIQUE numeric score between 0.00 and 1.00 (two decimals, as numbers). "
        "1.00 represents the most relevant term. "
        "Return JSON {{\"ranked\": [{{\"term\": \"...\", \"score\": 0.95}}, ...]}} sorted from highest to lowest score."
    )
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
    
    system = (
        f"You are a subject-matter expert in {subject_name}. Keep all subject-relevant terms unless they are obviously generic noise.\n\n"
        "Remove terms only if they are purely generic academic words (e.g., 'principle', 'concept', 'equation' by themselves) "
        "with no subject signal. KEEP multi-word phrases, named laws/equations, and domain nouns even if they look common. "
        "Err on the side of keeping terms unless they would clearly pollute retrieval.\n\n"
        "Return JSON with 'filtered_terms' containing the kept terms."
    )
    
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
    system = (
        "You help with textbook lookup. For each concept term, propose up to two "
        f"alternate keywords, abbreviations, or symbols that might appear in {subject} materials. "
        "Return a JSON object mapping each input term to an array of 0-2 strings."
    )
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

    system = (
        f"You are a {subject} textbook index builder. A student has asked a question "
        "and you must identify the specific concepts they need to look up.\n\n"
        "Perform these tasks:\n\n"
        "1. CONTEXT SUMMARY: Write a short sentence or comma-separated list naming "
        "the core topics, principles, or equations the question is about.\n\n"
        "2. CONCEPT EXTRACTION: Identify up to 8 lookup concepts that a student "
        "would search for in a textbook index to answer this question.\n\n"
        "   Rules for each concept:\n"
        "   - Each concept is 1 to 4 lowercase words.\n"
        "   - Phrase concepts as they would appear in a textbook index or section "
        "heading (e.g., \"boundary layer thickness\", \"Reynolds number\", "
        "\"conservation of momentum\").\n"
        "   - A multi-word concept must be a single coherent idea. Do NOT split "
        "a compound concept into its individual words as separate entries.\n"
        "   - Order from MOST SPECIFIC to MOST GENERAL:\n"
        "     First: the exact compound concept being asked about.\n"
        "     Then: closely related sub-concepts or prerequisite concepts.\n"
        "     Last: the broader topic area, only if it adds retrieval value.\n"
        "   - Include named laws, equations, phenomena, and domain-specific "
        "noun phrases.\n"
        "   - OMIT generic academic words that match too broadly on their own "
        "(e.g., \"equation\", \"method\", \"theory\" alone). These are acceptable "
        "ONLY as part of a named concept (e.g., \"Bernoulli equation\").\n"
        "   - Assign each concept a UNIQUE relevance score between 0.00 and 1.00 "
        "(1.00 = most directly answers the question).\n\n"
        "3. Think: \"If I were looking up this answer in a textbook, what section "
        "headings or index entries would I turn to?\"\n\n"
        "Return JSON exactly:\n"
        "{\"context_summary\": \"...\", "
        "\"ranked_terms\": [{\"term\": \"boundary layer thickness\", \"relevance\": 0.98}, ...]}\n"
        "Sorted highest to lowest relevance. Return at most 8 concepts."
    )

    payload = {"subject": subject, "question": question}

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
    system = (
        f"You are parsing {subject} textbook problems. "
        "Extract problem_type, asked_outputs, knowns, constraints, and figure_refs. "
        "Return ONLY JSON with keys: problem_type, asked_outputs, knowns, constraints, figure_refs."
    )
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


def solve_with_bundle(
    parsed_task: ParsedTask, bundle: ResearchBundle, hint: str | None = None,
    subject: str | None = None,
) -> ProposedSolution:
    """Solve the parsed task using only information from the provided bundle."""

    client = _client()
    question_text = ""
    try:
        question_text = getattr(bundle.metadata, "question", "") or getattr(
            bundle.metadata, "original_query", ""
        )
    except Exception:
        log.warning("Failed to extract question from bundle metadata")
        question_text = ""

    q = question_text or parsed_task.problem_type
    scorer_model = os.getenv(
        "CITATION_SCORER_MODEL", os.getenv("PARSER_MODEL", "gpt-4o")
    )

    # Build per-snippet args before submitting to the pool
    snippet_args: List[Tuple[Any, float, str]] = []
    for sn in bundle.snippets:
        focus_term = _pick_concept_term(sn)
        importance = _importance_from_snippet(sn)
        snippet_args.append((sn, importance, focus_term))

    # Run merged score+answer in parallel (Phase 2+3)
    max_workers = min(
        int(os.getenv("CITATION_WORKERS", "6")),
        max(len(snippet_args), 1),
    )
    combined_results: List[Dict[str, Any]] = []
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

    system = (
        "You are a Socratic subject-matter tutor. You are given SOURCE EXCERPTS from "
        "course materials, each with a citation marker and relevance score. "
        "Your job is to guide the student toward understanding using ONLY the information "
        "in these excerpts.\n\n"
        "TUTORING STYLE:\n"
        " - Guide the student toward understanding rather than giving a complete, "
        "encyclopedic answer.\n"
        " - Ask thought-provoking questions that help the student reason through the concept. "
        "For example, instead of stating a definition outright, lead with: "
        "'Consider what happens at the wall vs. far from the surface — what must the "
        "velocity do in between?'\n"
        " - After presenting a key idea, prompt the student to think further: "
        "'What do you think happens when...?' or 'Why do you think this matters for...?'\n"
        " - Build concepts step by step — define fundamentals before applications.\n"
        " - Use precise technical language. Avoid vague or anthropomorphic phrasing.\n"
        " - Keep the response focused on what was asked. Do not introduce tangential topics.\n\n"
        "STRICT RULES:\n"
        " - Base your response ONLY on the provided source excerpts. Do NOT add facts, "
        "equations, or claims from outside knowledge.\n"
        " - Cite every factual statement with its marker (e.g., [Textbook, p. X]).\n"
        " - If the source excerpts do not contain enough information to address the question, "
        "say so honestly: 'The available materials cover X but do not address Y.'\n"
        " - Do NOT perform numeric calculations, approximations, or substitutions.\n"
        " - Do NOT fabricate specific numbers, thresholds, or criteria not present in the excerpts."
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
    payload_lines.append("Return JSON with keys: steps, final_answers, equations_used, assumptions.")
    payload_lines.append(
        "- steps: a Socratic tutoring response that uses ONLY information from the source excerpts, "
        "with citations on every factual sentence. Guide the student rather than lecturing."
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

    raw_steps = data.get("steps", "")
    if not isinstance(raw_steps, str):
        try:
            raw_steps = json.dumps(raw_steps, ensure_ascii=False)
        except Exception:
            log.debug("Steps JSON serialization failed, using str()")
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
    "format_answer",
    "normalize_query",
    "is_question_subject_relevant",
    "extract_keywords",
    "filter_general_terms",
    "propose_synonyms",
    "_answer_single_snippet",
    "_write_miniresponses",
]
