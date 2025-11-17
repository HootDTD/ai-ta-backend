from __future__ import annotations

"""State machine orchestrating the closed-book QA pipeline."""

import hashlib
import json
import math
import os
import re
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Any, Set, Tuple
from difflib import SequenceMatcher

import tiktoken

from .contracts import (
    FinalAnswer,
    ParsedTask,
    Proof,
    ResearchBundle,
    ResearchMetadata,
    BundleSnippet,
)
from .config import get_subject_name
from .main_ai import (
    parse_question,
    normalize_query,
    is_question_subject_relevant,
    extract_keywords,
    filter_keywords_by_subject,
    propose_synonyms,
)
from .retriever import batch_lookup_terms, _summarize_snippets, _compute_equal_scores


WIRE = os.getenv("RETRIEVAL_WIRE_LOG", "off").lower() not in {"0","off","false","no"}

CITATION_PATTERN = re.compile(r"\[[^,\[\]]+,\s*p\.\s*[^\]]+\]")

VALUE_SEPARATORS = ("=", "≈", "~", "≃", "≅", ":")
NUMERIC_PREFIX = re.compile(r"^\s*[-+]?(\d|\.\d)")
MEASUREMENT_UNIT_TOKENS = {
    "m",
    "meter",
    "meters",
    "cm",
    "centimeter",
    "centimeters",
    "mm",
    "millimeter",
    "millimeters",
    "km",
    "kilometer",
    "kilometers",
    "ft",
    "foot",
    "feet",
    "in",
    "inch",
    "inches",
    "yd",
    "yard",
    "yards",
    "mi",
    "mile",
    "miles",
    "kg",
    "kilogram",
    "kilograms",
    "g",
    "gram",
    "grams",
    "slug",
    "lb",
    "lbs",
    "lbm",
    "pa",
    "kpa",
    "mpa",
    "psi",
    "bar",
    "atm",
    "torr",
    "degc",
    "degf",
    "w",
    "kw",
    "mw",
    "j",
    "kj",
    "mol",
    "mole",
    "moles",
    "l",
    "liter",
    "liters",
    "cc",
    "hz",
    "rpm",
    "cfm",
    "gpm",
    "lpm",
}


def _clamp_weight(value: float, default: float = 1.0) -> float:
    if value is None:
        val = default
    else:
        try:
            val = float(value)
        except (TypeError, ValueError):
            val = default
    if not math.isfinite(val):
        val = default
    return float(max(0.05, min(1.0, val)))


try:  # optional pint dependency for unit checks
    from pint import UnitRegistry

    ureg = UnitRegistry()
except Exception:  # pragma: no cover - pint may be absent
    ureg = None


class Orchestrator:
    """Sequential orchestrator with validation and retries."""

    def __init__(self, max_retrieval_rounds: int = 2, max_solve_rounds: int = 2):
        self.max_retrieval_rounds = max_retrieval_rounds
        self.max_solve_rounds = max_solve_rounds

    def _semantic_similarity(self, seed: str, candidate: str) -> float:
        """Return a normalized similarity score between ``seed`` and ``candidate``."""

        seed_clean = (seed or "").strip().lower()
        cand_clean = (candidate or "").strip().lower()
        if not seed_clean or not cand_clean:
            return 0.0

        ratio = SequenceMatcher(None, seed_clean, cand_clean).ratio()
        # Ensure a small positive floor so accepted semantic suggestions aren't zeroed out.
        if ratio < 0.3:
            ratio = 0.3
        return float(max(0.0, min(ratio, 1.0)))

    def _build_keyword_matrix(
        self, seed_terms: List[Any], skip_semantic: bool
    ) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
        """Return keyword-score pairs plus a record of semantic expansions."""

        keyword_matrix: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        base_weight_map: Dict[str, float] = {}
        processed_terms: List[str] = []

        for entry in seed_terms or []:
            if isinstance(entry, dict):
                raw_term = entry.get("term") or entry.get("keyword") or ""
                base_weight = entry.get("weight", 1.0)
            else:
                raw_term = entry
                base_weight = 1.0
            cleaned = (raw_term or "").strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                base_weight_map[key] = max(base_weight_map.get(key, 1.0), _clamp_weight(base_weight))
                continue
            weight_val = _clamp_weight(base_weight)
            keyword_matrix.append(
                {
                    "term": cleaned,
                    "score": weight_val,
                    "origin": "seed",
                    "source_term": cleaned,
                }
            )
            seen.add(key)
            processed_terms.append(cleaned)
            base_weight_map[key] = weight_val

        semantic_map: Dict[str, List[str]] = {}
        if skip_semantic or not processed_terms:
            return keyword_matrix, semantic_map

        raw_synonyms = propose_synonyms(processed_terms)
        base_lookup: Dict[str, str] = {term.lower(): term for term in processed_terms}
        normalized: Dict[str, List[str]] = {}

        if isinstance(raw_synonyms, dict):
            for raw_key, values in raw_synonyms.items():
                if not isinstance(raw_key, str):
                    continue
                base = base_lookup.get(raw_key.lower())
                if base is None:
                    continue
                if not isinstance(values, list):
                    continue
                normalized.setdefault(base, [])
                for cand in values:
                    if isinstance(cand, str):
                        normalized[base].append(cand)

        for base in processed_terms:
            candidates = normalized.get(base, [])
            base_seen: Set[str] = set()
            cleaned_candidates: List[str] = []
            for cand in candidates:
                cleaned = cand.strip()
                if not cleaned:
                    continue
                cand_key = cleaned.lower()
                if cand_key in base_seen or cand_key in seen:
                    continue
                base_weight = base_weight_map.get(base.lower(), 1.0)
                score = self._semantic_similarity(base, cleaned) * base_weight
                if score <= 0:
                    continue
                score = round(float(max(0.0, min(score, 1.0))), 3)
                keyword_matrix.append(
                    {
                        "term": cleaned,
                        "score": score,
                        "origin": "semantic",
                        "source_term": base,
                    }
                )
                seen.add(cand_key)
                base_seen.add(cand_key)
                cleaned_candidates.append(cleaned)
            if cleaned_candidates:
                semantic_map[base] = cleaned_candidates

        return keyword_matrix, semantic_map

    def _sanitize_term(self, term: str) -> str:
        """Normalize a single keyword candidate into a conceptual token."""

        return self._trim_measurement_tokens(self._strip_value_assignments(term))

    def _append_weighted_term(
        self,
        terms: List[str],
        weight_map: Dict[str, float],
        raw_term: str,
        weight: float,
    ) -> None:
        """Insert a sanitized term with an associated importance weight."""

        candidate = self._sanitize_term(raw_term)
        if not candidate:
            return
        key = candidate.lower()
        norm_weight = _clamp_weight(weight)
        if key in weight_map:
            weight_map[key] = max(weight_map.get(key, norm_weight), norm_weight)
            return
        weight_map[key] = norm_weight
        terms.append(candidate)

    def _apply_importance_weights(self, snippet_records: Dict[str, Dict[str, Any]]) -> None:
        """Boost snippet equal scores according to originating keyword importance."""

        for info in snippet_records.values():
            snippet = info.get("snippet")
            if not snippet:
                continue
            importance = _clamp_weight(info.get("importance", 1.0))
            fs = getattr(snippet, "final_score", {}) or {}
            equal_val = fs.get("equal")
            if equal_val is None:
                continue
            try:
                equal_float = float(equal_val)
            except (TypeError, ValueError):
                continue
            weighted_equal = equal_float * (importance ** 2)
            fs["equal_raw"] = equal_float
            fs["equal"] = weighted_equal
            fs["importance"] = importance
            setattr(snippet, "final_score", fs)

    def _strip_value_assignments(self, term: str) -> str:
        text = (term or "").strip()
        if not text:
            return ""
        for sep in VALUE_SEPARATORS:
            if sep in text:
                left, right = text.split(sep, 1)
                if re.search(r"\d", right):
                    text = left.strip(" ,;")
                    break
        return text

    def _trim_measurement_tokens(self, term: str) -> str:
        text = (term or "").strip(" ,;")
        if not text:
            return ""
        tokens = [tok for tok in re.split(r"\s+", text) if tok]
        tokens = self._drop_measure_tokens(tokens, from_start=True)
        tokens = self._drop_measure_tokens(tokens, from_start=False)
        candidate = " ".join(tokens).strip(" ,;")
        if not candidate:
            return ""
        words = re.findall(r"[A-Za-zµ°Ωα-ω]+", candidate)
        if words:
            normalized_words = [w.lower() for w in words]
            if all(word in MEASUREMENT_UNIT_TOKENS for word in normalized_words):
                return ""
        return candidate

    def _drop_measure_tokens(self, tokens: List[str], from_start: bool) -> List[str]:
        while tokens:
            idx = 0 if from_start else -1
            token = tokens[idx].strip(",;")
            if not token:
                tokens.pop(idx)
                continue
            if self._is_measurement_token(token):
                tokens.pop(idx)
                continue
            break
        return tokens

    def _is_measurement_token(self, token: str) -> bool:
        stripped = token.strip()
        if not stripped:
            return True
        if NUMERIC_PREFIX.match(stripped):
            return True
        letters_only = re.sub(r"[^A-Za-zµ°Ωα-ω]", "", stripped).lower()
        has_digits = any(ch.isdigit() for ch in stripped)
        if any(ch in stripped for ch in "°‰µ²³"):
            return True
        lowered = stripped.lower()
        if "/" in lowered or "*" in lowered or "·" in lowered or "×" in lowered:
            pieces = re.split(r"[*/·×]", lowered)
            cleaned = [
                re.sub(r"[^a-z]", "", piece)
                for piece in pieces
                if re.sub(r"[^a-z]", "", piece)
            ]
            if cleaned and all(piece in MEASUREMENT_UNIT_TOKENS for piece in cleaned):
                return True
        if "^" in stripped and letters_only in MEASUREMENT_UNIT_TOKENS:
            return True
        if letters_only and letters_only in MEASUREMENT_UNIT_TOKENS and not has_digits:
            return True
        if has_digits and letters_only and letters_only in MEASUREMENT_UNIT_TOKENS:
            first_char = stripped.strip()[0]
            if first_char.isdigit() or first_char in "+-.":
                return True
        return False

    def _question_matches_subject(self, question: str) -> bool:
        """Return True when the user question appears to reference the active subject."""

        try:
            return is_question_subject_relevant(question)
        except Exception:
            # Default to allowing the question so we do not block legitimate requests
            # when the classifier (or API) fails.
            return True

    def _iterative_research(
        self, question: str, options: Dict[str, Any], max_iters: int
    ) -> ResearchBundle:
        sanitize = os.getenv("RETRIEVAL_SANITIZE", "on").lower() not in {"0", "off", "false", "no"}
        norm_question = normalize_query(question) if sanitize else question

        subject_relevant = self._question_matches_subject(question)
        context_summary = ""
        if subject_relevant:
            context_summary = extract_keywords(question) or ""
            if WIRE:
                summary_for_log = context_summary.strip()
                log_value = (
                    json.dumps(summary_for_log, ensure_ascii=False)
                    if summary_for_log
                    else "null"
                )
                print(
                    f"[Main AI -> Indexer AI] context_sentences={log_value}",
                    flush=True,
                )
        else:
            if WIRE:
                print(
                    "[Main AI -> Indexer AI] context_sentences=null (question_out_of_scope)",
                    flush=True,
                )

        initial_terms: List[str] = []
        term_weights: Dict[str, float] = {}
        limited_seed = False
        skip_semantic_env = os.getenv("RETRIEVAL_SKIP_SYNONYMS", "off").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        skip_semantic = True

        if subject_relevant:
            filtered_terms = filter_keywords_by_subject(context_summary, question)
            limited_seed = bool(filtered_terms)
            fallback_needed = False
            if filtered_terms:
                sanitized_filtered: List[str] = []
                sanitized_weights: Dict[str, float] = {}
                seen_filtered: Set[str] = set()
                for entry in filtered_terms:
                    if isinstance(entry, dict):
                        raw_term = entry.get("term") or entry.get("keyword") or entry.get("name") or ""
                        rel = entry.get("relevance")
                    elif isinstance(entry, str):
                        raw_term = entry
                        rel = 1.0
                    else:
                        continue
                    sanitized = self._sanitize_term(raw_term)
                    if not sanitized:
                        continue
                    key = sanitized.lower()
                    weight_val = _clamp_weight(rel if rel is not None else 0.8)
                    if key in seen_filtered:
                        sanitized_weights[key] = max(sanitized_weights.get(key, weight_val), weight_val)
                        continue
                    seen_filtered.add(key)
                    sanitized_filtered.append(sanitized)
                    sanitized_weights[key] = weight_val
                if sanitized_filtered:
                    initial_terms = sanitized_filtered
                    term_weights = sanitized_weights
                else:
                    fallback_needed = True
            else:
                fallback_needed = True

            if fallback_needed or not initial_terms:
                tokens = []
                try:
                    import re as _re
                    tokens = [
                        t
                        for t in _re.findall(r"[A-Za-z][A-Za-z0-9_\-]+", norm_question or question)
                        if len(t) >= 3
                    ]
                except Exception:
                    tokens = []
                if tokens:
                    initial_terms = []
                    term_weights = {}
                    for tok in tokens[:6]:
                        self._append_weighted_term(initial_terms, term_weights, tok.lower(), 0.6)
                else:
                    fallback_text = context_summary or norm_question or question or ""
                    initial_terms = []
                    term_weights = {}
                    if fallback_text.strip():
                        self._append_weighted_term(
                            initial_terms, term_weights, fallback_text.strip().lower(), 0.6
                        )
            if not initial_terms:
                self._append_weighted_term(initial_terms, term_weights, "fluid mechanics", 0.5)

            if not limited_seed:
                skip_semantic = skip_semantic_env
        else:
            skip_semantic = skip_semantic_env

        seed_entries = [
            {"term": term, "weight": term_weights.get(term.lower(), 1.0)}
            for term in initial_terms
        ]
        keyword_matrix, semantic_map = self._build_keyword_matrix(seed_entries, skip_semantic)

        concept_order: List[str] = []
        concept_display: Dict[str, str] = {}
        term_to_concept: Dict[str, str] = {}
        concept_index_map: Dict[str, int] = {}
        term_origin: Dict[str, Dict[str, Any]] = {}

        for entry in keyword_matrix:
            term = entry.get("term", "")
            source_term = entry.get("source_term") or term
            concept_key = source_term.lower()
            if concept_key not in concept_display:
                concept_display[concept_key] = source_term
                concept_order.append(concept_key)
                concept_index_map[concept_key] = len(concept_order) - 1
            term_to_concept[term.lower()] = concept_key
            term_origin[term.lower()] = {
                "type": entry.get("origin", "seed"),
                "source": source_term,
                "score": entry.get("score", 1.0),
            }

        round_terms: List[str] = []
        seen_round: Set[str] = set()
        for entry in keyword_matrix:
            candidate = (entry.get("term") or "").strip()
            if not candidate:
                continue
            key = candidate.lower()
            if key in seen_round:
                continue
            seen_round.add(key)
            round_terms.append(candidate)

        if limited_seed and len(round_terms) > 8:
            score_map: Dict[str, float] = {}
            for entry in keyword_matrix:
                candidate = (entry.get("term") or "").strip()
                if not candidate:
                    continue
                score_map[candidate.lower()] = float(entry.get("score", term_weights.get(candidate.lower(), 0.0)))
            ranked_terms = sorted(
                round_terms,
                key=lambda term: score_map.get(term.lower(), term_weights.get(term.lower(), 0.0)),
                reverse=True,
            )
            round_terms = ranked_terms[:8]

        iterations: List[Dict[str, Any]] = []
        attempted_terms: List[str] = list(round_terms)
        concept_found: Set[str] = set()
        concept_matches: Dict[str, List[str]] = {}
        concept_match_details: Dict[str, List[Dict[str, Any]]] = {}
        concept_first_found_iter: Dict[str, int] = {}

        snippet_records: Dict[str, Dict[str, Any]] = {}

        loaded_indexes: Set[str] = set()
        skipped_entries: List[Dict[str, Any]] = []
        term_diagnostics: Dict[str, Dict[str, Any]] = {}

        if WIRE:
            print(
                f"[Main AI -> Indexer AI] pending={json.dumps(round_terms, ensure_ascii=False)}",
                flush=True,
            )

        if round_terms:
            # Ensure the indexer returns all citations for provided terms.
            _opts = dict(options or {})
            _opts["all_citations"] = True
            found_array, not_found_array, diag = batch_lookup_terms(round_terms, _opts)
        else:
            found_array = []
            not_found_array = []
            diag = {"loaded_indexes": [], "skipped_indexes": [], "per_term": {}}
        loaded_indexes.update(diag.get("loaded_indexes", []))
        skipped_entries.extend(diag.get("skipped_indexes", []))
        per_term_diag = diag.get("per_term", {}) or {}
        term_diagnostics.update(per_term_diag)

        found_terms = [item.get("term") for item in found_array if isinstance(item, dict)]
        if WIRE:
            print(
                "[Indexer AI -> Main AI] found="
                + json.dumps([t for t in found_terms if t], ensure_ascii=False)
                + " not_found="
                + json.dumps(list(not_found_array), ensure_ascii=False),
                flush=True,
            )

        iter_idx = 1
        max_rounds = 1
        iteration_entry: Dict[str, Any] = {
            "iter": iter_idx,
            "sent_terms": list(round_terms),
            "found_terms": [t for t in found_terms if t],
            "not_found_terms": list(not_found_array),
            "keyword_matrix": keyword_matrix,
            "semantic_expansion": semantic_map,
        }
        found_details: List[Dict[str, Any]] = []
        all_citation_markers: List[str] = []
        all_citation_seen: Set[str] = set()

        allowed_snippet_reasons = {"hit", "definition", "neighbor"}

        for result in found_array:
            term_label = result.get("term", "")
            key = term_label.lower()
            concept_key = term_to_concept.get(key, key)
            if concept_key not in concept_display:
                concept_display[concept_key] = term_label or concept_key
                concept_order.append(concept_key)
                concept_index_map[concept_key] = len(concept_order) - 1
            concept_found.add(concept_key)
            concept_matches.setdefault(concept_key, [])
            if term_label and term_label not in concept_matches[concept_key]:
                concept_matches[concept_key].append(term_label)

            concept_first_found_iter[concept_key] = min(
                concept_first_found_iter.get(concept_key, iter_idx), iter_idx
            )
            origin_info = term_origin.get(
                key,
                {"type": "seed", "source": term_label, "score": 1.0},
            )
            concept_match_details.setdefault(concept_key, []).append(
                {
                    "term": term_label,
                    "iteration": iter_idx,
                    "origin": origin_info.get("type", "seed"),
                    "source_term": origin_info.get("source"),
                    "score": origin_info.get("score", 1.0),
                }
            )
            diag_entry = per_term_diag.get(term_label) or per_term_diag.get(key) or {}
            alias_hits = [
                a for a in diag_entry.get("alias_hits", []) if isinstance(a, str)
            ]
            citation_markers: List[str] = []
            seen_markers: Set[str] = set()
            result_citations = result.get("citations") if isinstance(result, dict) else None
            if isinstance(result_citations, list):
                for marker in result_citations:
                    if isinstance(marker, str):
                        cleaned = marker.strip()
                        if cleaned and cleaned not in seen_markers:
                            seen_markers.add(cleaned)
                            citation_markers.append(cleaned)
                        if cleaned and cleaned not in all_citation_seen:
                            all_citation_seen.add(cleaned)
                            all_citation_markers.append(cleaned)
            if not citation_markers:
                for sn in result.get("snippets", []) if isinstance(result, dict) else []:
                    marker = getattr(sn, "citation_marker", None)
                    if isinstance(marker, str):
                        cleaned = marker.strip()
                        if cleaned and cleaned not in seen_markers:
                            seen_markers.add(cleaned)
                            citation_markers.append(cleaned)
                        if cleaned and cleaned not in all_citation_seen:
                            all_citation_seen.add(cleaned)
                            all_citation_markers.append(cleaned)

            found_details.append(
                {
                    "term": term_label,
                    "concept": concept_display.get(concept_key, term_label or concept_key),
                    "iteration": iter_idx,
                    "origin": origin_info.get("type", "seed"),
                    "source_term": origin_info.get("source"),
                    "score": origin_info.get("score", 1.0),
                    "citations": citation_markers,
                }
            )

            for sn in result.get("snippets", []):
                if not sn or not getattr(sn, "id", None):
                    continue
                reason = (getattr(sn, "why", "") or "").lower()
                if reason not in allowed_snippet_reasons:
                    continue
                existing = snippet_records.get(sn.id)
                text_len = len(getattr(sn, "text", "") or "")
                alias_hit_for_term = False
                if alias_hits:
                    text_lower = (getattr(sn, "text", "") or "").lower()
                    for alias in alias_hits:
                        alias_lower = alias.lower()
                        if alias_lower and alias_lower in text_lower:
                            alias_hit_for_term = True
                            break
                term_importance = _clamp_weight(origin_info.get("score", 1.0))
                if existing is None:
                    snippet_records[sn.id] = {
                        "snippet": sn,
                        "concept_rank": concept_index_map.get(
                            concept_key, len(concept_order)
                        ),
                        "first_iter": iter_idx,
                        "alias_hit": alias_hit_for_term,
                        "concepts": {concept_key},
                        "importance": term_importance,
                    }
                    continue

                if text_len > len(getattr(existing.get("snippet"), "text", "") or ""):
                    existing["snippet"] = sn
                existing["concept_rank"] = min(
                    existing.get("concept_rank", concept_index_map.get(concept_key, len(concept_order))),
                    concept_index_map.get(concept_key, len(concept_order)),
                )
                existing["first_iter"] = min(existing.get("first_iter", iter_idx), iter_idx)
                existing.setdefault("alias_hit", False)
                existing["alias_hit"] = existing["alias_hit"] or alias_hit_for_term
                existing.setdefault("concepts", set()).add(concept_key)
                existing["importance"] = max(existing.get("importance", term_importance), term_importance)

        iteration_entry["found_details"] = found_details
        iterations.append(iteration_entry)

        token_budget = int(options.get("token_budget", 6000))
        enc = tiktoken.get_encoding("cl100k_base")
        snippet_infos: List[Dict[str, Any]] = []
        allowed_snippets = [
            info.get("snippet")
            for info in snippet_records.values()
            if getattr(info.get("snippet"), "why", "").lower() in allowed_snippet_reasons
        ]
        if allowed_snippets:
            _compute_equal_scores(allowed_snippets)
            self._apply_importance_weights(snippet_records)
        for sn_id, info in snippet_records.items():
            snippet = info.get("snippet")
            if not snippet:
                continue
            if (getattr(snippet, "why", "") or "").lower() not in allowed_snippet_reasons:
                continue
            scores = getattr(snippet, "final_score", {}) or {}
            equal_raw = scores.get("equal")
            try:
                equal_score = float(equal_raw)
            except Exception:
                equal_score = float("-inf")
            concepts = info.get("concepts", set()) or {""}
            concept_iter = min(
                concept_first_found_iter.get(c, max_rounds + 10) for c in concepts
            )
            concept_rank = min(
                concept_index_map.get(c, len(concept_order)) for c in concepts
            )
            alias_priority = 0 if info.get("alias_hit") else 1
            token_count = len(enc.encode(getattr(snippet, "text", "") or ""))
            concept_labels = [
                concept_display.get(c, c)
                for c in sorted(info.get("concepts", set()) or set())
                if concept_display.get(c, c)
            ]
            snippet_infos.append(
                {
                    "id": sn_id,
                    "snippet": snippet,
                    "concept_iter": concept_iter,
                    "concept_rank": concept_rank,
                    "alias_priority": alias_priority,
                    "token_count": token_count,
                    "equal_score": equal_score,
                    "concept_terms": concept_labels,
                }
            )

        snippet_infos.sort(
            key=lambda entry: (
                -entry["equal_score"],
                entry["alias_priority"],
                entry["concept_iter"],
                entry["concept_rank"],
                entry["token_count"],
                entry["id"],
            )
        )

        max_snippets = 20
        truncated = len(snippet_infos) > max_snippets
        snippet_infos = snippet_infos[:max_snippets]

        kept_snippets: List[BundleSnippet] = []
        for entry in snippet_infos:
            snippet = entry["snippet"]
            concept_terms = entry.get("concept_terms") or []
            if concept_terms:
                setattr(snippet, "concept_terms", concept_terms)
            kept_snippets.append(snippet)
        total_tokens = sum(entry["token_count"] for entry in snippet_infos)
        if total_tokens > token_budget:
            truncated = True

        allowed_markers: List[str] = []
        seen_markers: Set[str] = set()
        for sn in kept_snippets:
            marker = getattr(sn, "citation_marker", None)
            if isinstance(marker, str):
                cleaned = marker.strip()
                if cleaned and cleaned not in seen_markers:
                    seen_markers.add(cleaned)
                    allowed_markers.append(cleaned)
        for marker in all_citation_markers:
            if marker not in seen_markers:
                seen_markers.add(marker)
                allowed_markers.append(marker)

        equations, glossary, assumptions, alias_counts_total = _summarize_snippets(
            kept_snippets
        )
        stats = {
            "tokens": total_tokens,
            "token_budget": token_budget,
            "truncated": truncated,
        }
        doc_sets = options.get("doc_sets") or []
        k_sem = int(options.get("k_sem", 0))
        k_lex = int(options.get("k_lex", 0))

        found_terms_meta = [
            concept_display[key]
            for key in concept_order
            if key in concept_found and key in concept_display
        ]
        not_found_terms = [
            concept_display[key]
            for key in concept_order
            if key not in concept_found and key in concept_display
        ]

        skipped_unique: List[Dict[str, Any]] = []
        seen_paths: Set[str] = set()
        for entry in skipped_entries:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if path and path in seen_paths:
                continue
            if path:
                seen_paths.add(path)
            skipped_unique.append(entry)

        term_presence = {
            term: diag.get("term_presence", {})
            for term, diag in term_diagnostics.items()
            if isinstance(diag, dict)
        }
        expansion_candidates = {
            term: diag.get("expansion_candidates", [])
            for term, diag in term_diagnostics.items()
            if isinstance(diag, dict)
        }
        hit_count_sem = sum(
            int(diag.get("hit_count_sem", 0) or 0)
            for diag in term_diagnostics.values()
            if isinstance(diag, dict)
        )
        hit_count_lex = sum(
            int(diag.get("hit_count_lex", 0) or 0)
            for diag in term_diagnostics.values()
            if isinstance(diag, dict)
        )

        metadata_dict = {
            "doc_sets": doc_sets,
            "loaded_indexes": sorted(loaded_indexes),
            "skipped_indexes": skipped_unique,
            "question": norm_question,
            "original_query": question,
            "k_sem": k_sem,
            "k_lex": k_lex,
            "token_budget": token_budget,
            "term_presence": term_presence,
            "expansion_candidates": expansion_candidates,
            "hit_count_sem": hit_count_sem,
            "hit_count_lex": hit_count_lex,
            "aliases_used": alias_counts_total,
            "iteration_trace": iterations,
            "keyword_iterations": iterations,
            "found_terms": found_terms_meta,
            "not_found_terms": not_found_terms,
            "attempted_terms": attempted_terms,
            "missing_terms": not_found_terms,
            "final_query": " ".join(round_terms),
            "concept_matches": concept_matches,
            "concept_match_details": concept_match_details,
            "coverage_gaps": [],
            "refinement_queries": [],
            "subject": get_subject_name(),
            "allowed_markers": allowed_markers,
        }

        metadata = ResearchMetadata(**metadata_dict)
        bundle = ResearchBundle(
            metadata=metadata,
            snippets=kept_snippets,
            equations=equations,
            assumptions=assumptions,
            glossary=glossary,
            coverage_gaps=[],
            refinement_queries=[],
            used_ids=[sn.id for sn in kept_snippets],
            stats=stats,
            provenance={"source": "keyword_loop"},
            allowed_markers=allowed_markers,
            found_terms=found_terms_meta,
            not_found_terms=not_found_terms,
            attempted_terms=attempted_terms,
            subject=get_subject_name(),
        )
        return bundle

    def _hash(self, obj: object) -> str:
        """Deterministic hash for dataclasses.

        Any sets should be converted to sorted lists before hashing to avoid
        non-deterministic ordering affecting the digest.
        """

        def _clean(o):
            if isinstance(o, dict):
                return {str(k): _clean(v) for k, v in o.items()}
            if isinstance(o, list):
                return [_clean(v) for v in o]
            if isinstance(o, set):  # ensure stable ordering
                return sorted(_clean(v) for v in o)
            return o

        return hashlib.sha256(
            json.dumps(_clean(asdict(obj)), sort_keys=True).encode()
        ).hexdigest()

    def _normalize_eq(self, text: str) -> str:
        t = re.sub(r"\s+", " ", text.strip().rstrip(".;,"))
        t = re.sub(r"\s*\(\d+-\d+\)\s*$", "", t)  # drop trailing eq numbers
        if t.startswith("(") and t.endswith(")"):
            t = t[1:-1]
        return t

    def _validate_solution(
        self, sol: ProposedSolution, parsed: ParsedTask, bundle: ResearchBundle
    ) -> List[str]:
        issues: List[str] = []

        # Normalize steps into a string for downstream processing
        steps_obj = getattr(sol, "steps", "")
        if isinstance(steps_obj, list):
            parts: List[str] = []
            for part in steps_obj:
                if isinstance(part, str):
                    parts.append(part)
                else:
                    try:
                        parts.append(json.dumps(part))
                    except Exception:
                        parts.append(str(part))
            steps_text = "\n".join(parts)
        elif isinstance(steps_obj, str):
            steps_text = steps_obj
        elif steps_obj is None:
            steps_text = ""
        else:
            steps_text = str(steps_obj)

        paragraphs = [p for p in steps_text.split("\n") if p.strip()]
        for idx, para in enumerate(paragraphs, start=1):
            stripped = para.strip()
            if stripped.startswith("```"):
                continue  # skip fenced code blocks
            letters = re.findall(r"[A-Za-z]", stripped)
            if len(letters) < 5:  # treat as math-heavy; no citation needed
                continue
            if not CITATION_PATTERN.search(para):
                issues.append(f"missing citation in paragraph {idx}")
        asked_keys = getattr(parsed, "asked_output_keys", []) or []
        if not asked_keys:
            asked_raw = getattr(parsed, "asked_outputs", [])
            if isinstance(asked_raw, list):
                asked_list = asked_raw
            elif isinstance(asked_raw, str) and asked_raw.strip():
                asked_list = [asked_raw]
            else:
                asked_list = []

            def _slug(s: str) -> str:
                return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

            asked_keys = [_slug(a) for a in asked_list]
        # Normalize final answers to a mapping (conceptual mode keeps this empty)
        final_answers_obj = getattr(sol, "final_answers", {})
        if isinstance(final_answers_obj, dict):
            final_answers = final_answers_obj
        elif isinstance(final_answers_obj, list):
            final_answers = {}
            auto_idx = 0
            for entry in final_answers_obj:
                key = None
                value = entry
                if isinstance(entry, dict):
                    if "key" in entry and "value" in entry:
                        key = str(entry["key"])
                        value = entry["value"]
                    elif len(entry) == 1:
                        k, v = next(iter(entry.items()))
                        key = str(k)
                        value = v
                if key is None:
                    key = str(auto_idx)
                    auto_idx += 1
                final_answers[key] = value
        elif final_answers_obj:
            final_answers = {"answer": final_answers_obj}
        else:
            final_answers = {}

        if final_answers and asked_keys:
            for key in asked_keys:
                if key not in final_answers:
                    issues.append(f"answer lacks {key}")

        bundle_eqs = {self._normalize_eq(e["eq_text"]) for e in bundle.equations}
        eqs_raw = getattr(sol, "equations_used", [])
        if isinstance(eqs_raw, list):
            eq_list = [str(e) for e in eqs_raw]
        elif eqs_raw:
            eq_list = [str(eqs_raw)]
        else:
            eq_list = []
        for eq in eq_list:
            if self._normalize_eq(eq) not in bundle_eqs:
                issues.append(f"equation '{eq}' not in bundle")
        return issues

    def run(self, user_task: Dict[str, object]) -> FinalAnswer:
        start = datetime.utcnow().isoformat()
        question: str = str(user_task.get("user_query", ""))
        doc_sets: List[str] = list(user_task.get("doc_sets", [])) or []
        k_sem = int(user_task.get("k_sem", 30))
        k_lex = int(user_task.get("k_lex", 30))
        token_budget = int(user_task.get("token_budget", 6000))

        parsed: ParsedTask = parse_question(question)

        def _to_list(val: object) -> List[str]:
            if isinstance(val, list):
                return val
            if isinstance(val, str) and val.strip():
                return [val]
            return []

        def _to_dict(val: object) -> Dict[str, object]:
            return val if isinstance(val, dict) else {}

        parsed.asked_outputs = _to_list(parsed.asked_outputs)
        parsed.constraints = _to_list(parsed.constraints)
        parsed.figure_refs = _to_list(parsed.figure_refs)
        parsed.knowns = _to_dict(parsed.knowns)

        if not parsed.asked_outputs and not getattr(parsed, "asked_output_keys", []):
            tokens = re.findall(r"\b([A-Za-z][A-Za-z0-9_]*?)\b", question)
            found = [t for t in tokens if t in {"v_exit", "Q"} or "_" in t]
            if not found and re.search(r"\(1\).*?\(2\)", question, re.S):
                found = ["output_1", "output_2"]
            if found:
                parsed.asked_outputs = found
                parsed.asked_output_keys = [re.sub(r"[^a-z0-9]+", "_", f).lower() for f in found]

        with open("parsed_task.json", "w", encoding="utf-8") as f:
            json.dump(asdict(parsed), f, indent=2, ensure_ascii=False)
        parsed_hash = self._hash(parsed)

        bundle: ResearchBundle | None = None
        retrieval_opts = {
            "doc_sets": doc_sets,
            "k_sem": k_sem,
            "k_lex": k_lex,
            "token_budget": token_budget,
        }
        max_iters_raw = user_task.get("max_iters", os.getenv("RETRIEVAL_MAX_ITERS", "5"))
        try:
            max_iters_val = int(max_iters_raw)
        except (TypeError, ValueError):
            max_iters_val = 5
        max_iters = max(1, min(max_iters_val, 5))
        last_err = ""
        for _ in range(self.max_retrieval_rounds):
            bundle = self._iterative_research(question, retrieval_opts, max_iters)
            with open("bundle.json", "w", encoding="utf-8") as f:
                json.dump(asdict(bundle), f, indent=2, ensure_ascii=False)
            if not bundle.snippets:
                return FinalAnswer(
                    text="Not found in the approved materials.", citations=[]
                )
            try:
                bundle.validate()
                break
            except Exception as exc:
                last_err = str(exc)
                retrieval_opts["token_budget"] *= 2
                retrieval_opts["k_sem"] = min(retrieval_opts.get("k_sem", 30) * 2, 100)
                retrieval_opts["k_lex"] = min(
                    retrieval_opts.get("k_lex", 30) * 2, 100
                )
        else:
            raise RuntimeError(f"insufficient book context: {last_err}")
        if bundle is None:
            raise RuntimeError("retrieval failed")
        bundle_hash = self._hash(bundle)

        payload = {
            "question": question,
            "parsed_task": asdict(parsed),
            "research_bundle": asdict(bundle),
        }

        payload_text = json.dumps(payload, indent=2, ensure_ascii=False)

        citations: List[str] = []
        seen_markers: Set[str] = set()
        for sn in bundle.snippets:
            marker = getattr(sn, "citation_marker", None)
            if not isinstance(marker, str):
                continue
            cleaned = marker.strip()
            if cleaned and cleaned not in seen_markers:
                seen_markers.add(cleaned)
                citations.append(cleaned)

        models = {
            "parser": os.getenv("PARSER_MODEL", "gpt-4o"),
            "main": None,
        }
        proof = Proof(
            question=question,
            parsed_task=asdict(parsed),
            research_bundle=payload["research_bundle"],
            solver_output={},
            checks={
                "solution_valid": False,
                "issues": ["solver_skipped"],
                "skipped_indexes": getattr(bundle.metadata, "skipped_indexes", []),
                "models": models,
                "retrieval_params": {
                    "k_sem": getattr(bundle.metadata, "k_sem", None),
                    "k_lex": getattr(bundle.metadata, "k_lex", None),
                    "token_budget": getattr(bundle.metadata, "token_budget", None),
                },
            },
            indexes_used=doc_sets,
            timestamps={"start": start, "end": datetime.utcnow().isoformat()},
        )
        proof_data = asdict(proof)
        proof_data["hashes"] = {
            "parsed_task": parsed_hash,
            "bundle": bundle_hash,
        }
        with open("proof.json", "w", encoding="utf-8") as f:
            json.dump(proof_data, f, indent=2, ensure_ascii=False)
        return FinalAnswer(text=payload_text, citations=citations)


__all__ = ["Orchestrator"]
