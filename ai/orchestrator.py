from __future__ import annotations

"""State machine orchestrating the closed-book QA pipeline."""

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Any, Set, Tuple
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

import tiktoken

from ..config.contracts import (
    FinalAnswer,
    ParsedTask,
    Proof,
    ResearchBundle,
    ResearchMetadata,
    BundleSnippet,
)
from ..config.settings import get_subject_name, RequestConfig
from .main_ai import (
    parse_question,
    normalize_query,
    is_question_subject_relevant,
    extract_keywords,
    extract_and_filter_keywords,
    filter_keywords_by_subject,
    filter_general_terms,
    propose_synonyms,
)


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

    def __init__(self, max_retrieval_rounds: int = 2, max_solve_rounds: int = 2, ctx=None, cfg=None):
        self.max_retrieval_rounds = max_retrieval_rounds
        self.max_solve_rounds = max_solve_rounds
        self.ctx = ctx
        self.cfg = cfg

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
            log.error("Subject relevance check failed, defaulting to allow", exc_info=True)
            # Default to allowing the question so we do not block legitimate requests
            # when the classifier (or API) fails.
            return True

    def _canonical_term(self, term: str) -> str:
        """Return a normalized, lowercase term without possessives or extra punctuation."""

        cleaned = (term or "").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"[\"“”‘’´`]", "", cleaned)
        cleaned = re.sub(r"\b([A-Za-z]+)'s\b", r"\1", cleaned)
        cleaned = re.sub(r"[^\w\s\-]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned.lower()

    def _tokenize_words(self, text: str) -> List[str]:
        return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9\-']+", text or "") if w]

    def _candidate_compounds(self, text: str) -> List[str]:
        """Return bigram/trigram candidates from text (lowercase, underscore joined)."""

        tokens = [t.lower() for t in self._tokenize_words(text)]
        phrases: Set[str] = set()
        for n in (2, 3):
            for i in range(len(tokens) - n + 1):
                window = tokens[i : i + n]
                if len(window) != n:
                    continue
                if all(len(tok) <= 2 for tok in window):
                    continue
                phrase = "_".join(window)
                phrases.add(phrase)
        return list(phrases)

    def _is_standalone_valid(self, term: str) -> bool:
        """Heuristic standalone check without subject-specific word lists."""

        if not term:
            return False
        letters = re.findall(r"[A-Za-z]", term)
        if len(letters) < 3:
            return False
        ratio = len(letters) / max(len(term), 1)
        if ratio < 0.6:
            return False
        tokens = term.split("_")
        if len(tokens) == 1:
            if len(tokens[0]) <= 2:
                return False
        return True

    def _semantic_normalize_terms(
        self,
        terms: List[Dict[str, Any]] | List[str],
        context_summary: str,
        question: str,
    ) -> List[Dict[str, Any]]:
        """Preserve meaningful compounds and drop standalone-weak tokens unless needed."""

        mode = os.getenv("TERM_SEMANTIC_MODE", "hybrid").lower()
        if mode not in {"strict", "aggressive", "hybrid"}:
            mode = "hybrid"

        # Normalize incoming entries
        normalized: List[tuple[str, float]] = []
        seen: Set[str] = set()
        for entry in terms or []:
            if isinstance(entry, dict):
                raw_term = entry.get("term") or entry.get("keyword") or entry.get("name")
                rel = entry.get("relevance", 1.0)
            else:
                raw_term = entry
                rel = 1.0
            if not isinstance(raw_term, str):
                continue
            canon = self._canonical_term(raw_term)
            if not canon:
                continue
            if canon in seen:
                continue
            seen.add(canon)
            try:
                rel_val = float(rel)
            except Exception:
                log.debug("Relevance float conversion failed, defaulting to 1.0")
                rel_val = 1.0
            normalized.append((canon, rel_val))

        if not normalized:
            return []

        # Build compound candidates from context/question and adjacency of provided terms
        corpus_text = " ".join([context_summary or "", question or ""])
        corpus_compounds = set(self._candidate_compounds(corpus_text))
        adjacency_compounds: Set[str] = set()
        for i in range(len(normalized) - 1):
            first, _ = normalized[i]
            second, _ = normalized[i + 1]
            candidate = f"{first}_{second}"
            adjacency_compounds.add(candidate)
        compounds = corpus_compounds | adjacency_compounds

        # Score standalone validity
        valid_standalone: Set[str] = set()
        weak_terms: Set[str] = set()
        for term, _ in normalized:
            if self._is_standalone_valid(term):
                valid_standalone.add(term)
            else:
                weak_terms.add(term)

        preserved: List[Dict[str, Any]] = []
        dropped: List[str] = []

        # Preserve compounds if components weak but compound present
        used_compounds: Set[str] = set()
        for comp in compounds:
            parts = comp.split("_")
            if len(parts) < 2:
                continue
            if not any(p in weak_terms for p in parts):
                continue
            used_compounds.add(comp)

        # Build final list
        i = 0
        normalized_map: Dict[str, float] = {t: w for t, w in normalized}
        while i < len(normalized):
            term, weight = normalized[i]
            maybe_comp = None
            if i + 1 < len(normalized):
                pair = f"{term}_{normalized[i+1][0]}"
                if pair in used_compounds or (mode == "strict" and pair in compounds):
                    maybe_comp = pair
                    weight = max(weight, normalized[i+1][1])
                    i += 2
                else:
                    i += 1
            else:
                i += 1

            if maybe_comp:
                preserved.append({"term": maybe_comp, "relevance": weight})
                continue

            if term in valid_standalone:
                preserved.append({"term": term, "relevance": weight})
            else:
                if mode == "aggressive":
                    dropped.append(term)
                elif mode == "strict":
                    dropped.append(term)
                else:  # hybrid: keep weak terms only if no compounds consumed them
                    dropped.append(term)

        if WIRE and dropped:
            print(f"[Main AI -> Semantic Filter] dropped_terms={sorted(dropped)}", flush=True)
        if WIRE and used_compounds:
            print(f"[Main AI -> Semantic Filter] preserved_compounds={sorted(used_compounds)}", flush=True)

        return preserved

    def _iterative_research(
        self, question: str, options: Dict[str, Any]
    ) -> ResearchBundle:
        """Run the pgvector retrieval pipeline and return a ResearchBundle."""
        return self._retrieve(question, options)

    def _retrieve(
        self, question: str, options: Dict[str, Any]
    ) -> ResearchBundle:
        """Run the pgvector retrieval pipeline and return a ResearchBundle."""
        from ..database.session import run_async
        from ..retrieval.pipeline import retrieve_for_question
        from ..retrieval.context_packer import _summarize_snippets

        subject_name = self.cfg.subject_name if self.cfg else get_subject_name()

        # Relevance guard — keep AI-TA's closed-knowledge enforcement
        subject_relevant = self._question_matches_subject(question)
        if not subject_relevant:
            if WIRE:
                print(
                    "[Main AI -> Indexer AI] context_sentences=null (question_out_of_scope)",
                    flush=True,
                )
            # Return empty bundle — solve_with_bundle will handle out-of-scope gracefully
            metadata = ResearchMetadata(
                final_query=question,
                original_query=question,
                attempted_terms=[question],
                subject=subject_name,
            )
            return ResearchBundle(metadata=metadata, snippets=[], subject=subject_name)

        # Extract keywords as hints (not standalone search targets)
        try:
            _ctx_summary, filtered_terms = extract_and_filter_keywords(
                question, subject=subject_name
            )
        except Exception:
            filtered_terms = []

        keywords: List[str] = []
        for entry in (filtered_terms or []):
            if isinstance(entry, dict):
                term = entry.get("term") or entry.get("keyword") or entry.get("name")
                if isinstance(term, str) and term.strip():
                    keywords.append(term.strip())
            elif isinstance(entry, str) and entry.strip():
                keywords.append(entry.strip())

        # Retrieve search_space_id and db_session from ctx (set by server.py)
        ctx = self.ctx or {}
        search_space_id = None
        db_session = None
        if hasattr(ctx, "search_space_id"):
            search_space_id = ctx.search_space_id
            db_session = getattr(ctx, "db_session", None)
        elif isinstance(ctx, dict):
            search_space_id = ctx.get("search_space_id")
            db_session = ctx.get("db_session")

        if not search_space_id:
            log.error(
                "_iterative_research_pgvector: search_space_id missing in ctx — "
                "returning empty bundle. Set ctx on Orchestrator from server.py."
            )
            metadata = ResearchMetadata(
                final_query=question, original_query=question,
                attempted_terms=[question], subject=subject_name,
            )
            return ResearchBundle(metadata=metadata, snippets=[], subject=subject_name)

        token_budget = int(options.get("token_budget", 6000))
        weight_overrides = options.get("weight_overrides") or {}

        async def _run():
            from ..database.session import get_async_session
            if db_session is not None:
                return await retrieve_for_question(
                    query=question,
                    keywords=keywords,
                    search_space_id=search_space_id,
                    db_session=db_session,
                    weight_overrides=weight_overrides,
                    top_k=int(options.get("k_sem", 20)),
                    token_budget=token_budget,
                )
            async with get_async_session() as sess:
                return await retrieve_for_question(
                    query=question,
                    keywords=keywords,
                    search_space_id=search_space_id,
                    db_session=sess,
                    weight_overrides=weight_overrides,
                    top_k=int(options.get("k_sem", 20)),
                    token_budget=token_budget,
                )

        snippets, diagnostics = run_async(_run())

        if WIRE:
            print(
                f"[Indexer AI -> Main AI] found={len(snippets)} snippets via pgvector",
                flush=True,
            )

        equations, glossary, assumptions, _ = _summarize_snippets(snippets)
        allowed_markers = [sn.citation_marker for sn in snippets if sn.citation_marker]

        metadata = ResearchMetadata(
            keyword_iterations=[{
                "round": 1,
                "combined_query": diagnostics.get("combined_query", question),
            }],
            found_terms=keywords,
            not_found_terms=[],
            attempted_terms=[question],
            missing_terms=[],
            final_query=diagnostics.get("combined_query", question),
            original_query=question,
            concept_matches={},
            concept_match_details={},
            coverage_gaps=[],
            refinement_queries=[],
            subject=subject_name,
            allowed_markers=allowed_markers,
            hit_count_sem=diagnostics.get("hit_count_sem", 0),
        )

        return ResearchBundle(
            metadata=metadata,
            snippets=snippets,
            equations=equations,
            assumptions=assumptions,
            glossary=glossary,
            coverage_gaps=[],
            refinement_queries=[],
            used_ids=[sn.id for sn in snippets],
            stats=diagnostics,
            provenance={"source": "pgvector"},
            allowed_markers=allowed_markers,
            found_terms=keywords,
            not_found_terms=[],
            attempted_terms=[question],
            subject=subject_name,
        )

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
                        log.debug("Step JSON serialization failed, using str()")
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
        last_err = ""
        for _ in range(self.max_retrieval_rounds):
            bundle = self._iterative_research(question, retrieval_opts)
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
                log.warning("Bundle validation failed (attempt), retrying: %s", exc)
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
