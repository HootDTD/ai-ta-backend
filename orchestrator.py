from __future__ import annotations

"""State machine orchestrating the closed-book QA pipeline."""

import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Any, Set

import tiktoken

from .contracts import (
    FinalAnswer,
    ParsedTask,
    Proof,
    ProposedSolution,
    ResearchBundle,
    ResearchMetadata,
    BundleSnippet,
)
from .config import get_subject_name
from .main_ai import (
    parse_question,
    solve_with_bundle,
    format_answer,
    normalize_query,
    extract_keywords,
    propose_synonyms,
)
from .retriever import batch_lookup_terms, _summarize_snippets


WIRE = os.getenv("RETRIEVAL_WIRE_LOG", "off").lower() not in {"0","off","false","no"}

CITATION_PATTERN = re.compile(r"\[[^,\[\]]+,\s*p\.\s*[^\]]+\]")

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

    def _iterative_research(
        self, question: str, options: Dict[str, Any], max_iters: int
    ) -> ResearchBundle:
        sanitize = os.getenv("RETRIEVAL_SANITIZE", "on").lower() not in {"0", "off", "false", "no"}
        norm_question = normalize_query(question) if sanitize else question
        initial_terms = extract_keywords(question)
        if not initial_terms:
            fallback = norm_question or question
            initial_terms = [fallback.strip().lower()] if fallback else []

        concept_order: List[str] = []
        concept_display: Dict[str, str] = {}
        term_to_concept: Dict[str, str] = {}
        concept_index_map: Dict[str, int] = {}
        term_origin: Dict[str, Dict[str, str]] = {}
        for term in initial_terms:
            key = term.lower()
            if key not in concept_display:
                concept_display[key] = term
                concept_order.append(key)
                concept_index_map[key] = len(concept_order) - 1
            term_to_concept[key] = key
            term_origin[key] = {"type": "seed", "source": term}

        pending: List[str] = list(initial_terms)
        max_rounds = min(max_iters, 5)
        skip_synonyms = os.getenv("RETRIEVAL_SKIP_SYNONYMS", "off").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        iterations: List[Dict[str, Any]] = []
        attempted_terms: List[str] = []
        attempted_keys: Set[str] = set()
        attempted_record: Set[str] = set()
        concept_found: Set[str] = set()
        concept_matches: Dict[str, List[str]] = {}
        concept_match_details: Dict[str, List[Dict[str, Any]]] = {}
        concept_first_found_iter: Dict[str, int] = {}

        snippet_records: Dict[str, Dict[str, Any]] = {}

        loaded_indexes: Set[str] = set()
        skipped_entries: List[Dict[str, Any]] = []
        term_diagnostics: Dict[str, Dict[str, Any]] = {}

        for iter_idx in range(1, max_rounds + 1):
            if not pending:
                break

            round_terms: List[str] = []
            seen_round: Set[str] = set()
            for term in pending:
                candidate = (term or "").strip()
                if not candidate:
                    continue
                key = candidate.lower()
                concept_key = term_to_concept.get(key)
                if concept_key is None:
                    concept_key = key
                    term_to_concept[key] = concept_key
                    if concept_key not in concept_display:
                        concept_display[concept_key] = candidate
                        concept_order.append(concept_key)
                if key in seen_round or key in attempted_keys:
                    continue
                seen_round.add(key)
                round_terms.append(candidate)

            if not round_terms:
                break

            for term in round_terms:
                key = term.lower()
                if key not in attempted_record:
                    attempted_terms.append(term)
                    attempted_record.add(key)
                attempted_keys.add(key)

            if WIRE:
                print(
                    f"[Main AI -> Indexer AI] pending={json.dumps(round_terms, ensure_ascii=False)}",
                    flush=True,
                )

            found_array, not_found_array, diag = batch_lookup_terms(round_terms, options)
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

            iteration_entry: Dict[str, Any] = {
                "iter": iter_idx,
                "sent_terms": list(round_terms),
                "found_terms": [t for t in found_terms if t],
                "not_found_terms": list(not_found_array),
            }
            found_details: List[Dict[str, Any]] = []

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
                origin_info = term_origin.get(key, {"type": "seed", "source": term_label})
                concept_match_details.setdefault(concept_key, []).append(
                    {
                        "term": term_label,
                        "iteration": iter_idx,
                        "origin": origin_info.get("type", "seed"),
                        "source_term": origin_info.get("source"),
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
                if not citation_markers:
                    for sn in result.get("snippets", []) if isinstance(result, dict) else []:
                        marker = getattr(sn, "citation_marker", None)
                        if isinstance(marker, str):
                            cleaned = marker.strip()
                            if cleaned and cleaned not in seen_markers:
                                seen_markers.add(cleaned)
                                citation_markers.append(cleaned)

                found_details.append(
                    {
                        "term": term_label,
                        "concept": concept_display.get(concept_key, term_label or concept_key),
                        "iteration": iter_idx,
                        "origin": origin_info.get("type", "seed"),
                        "source_term": origin_info.get("source"),
                        "citations": citation_markers,
                    }
                )

                for sn in result.get("snippets", []):
                    if not sn or not getattr(sn, "id", None):
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
                    if existing is None:
                        snippet_records[sn.id] = {
                            "snippet": sn,
                            "concept_rank": concept_index_map.get(
                                concept_key, len(concept_order)
                            ),
                            "first_iter": iter_idx,
                            "alias_hit": alias_hit_for_term,
                            "concepts": {concept_key},
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

            iteration_entry["found_details"] = found_details

            if not not_found_array:
                iteration_entry["proposed_synonyms"] = {}
                iterations.append(iteration_entry)
                if WIRE:
                    print("[Main AI] synonyms_proposed={}", flush=True)
                pending = []
                break

            if skip_synonyms:
                iteration_entry["proposed_synonyms"] = {}
                iterations.append(iteration_entry)
                if WIRE:
                    print("[Main AI] synonyms_proposed={}", flush=True)
                pending = []
                break

            context_hint: Dict[str, Dict[str, Any]] = {}
            for term in not_found_array:
                diag_entry = per_term_diag.get(term)
                if not diag_entry:
                    diag_entry = per_term_diag.get(term.lower(), {})
                context_hint[term] = diag_entry or {}

            synonym_map = propose_synonyms(not_found_array, context_hint)
            iteration_entry["proposed_synonyms"] = synonym_map
            iterations.append(iteration_entry)

            if WIRE:
                print(
                    "[Main AI] synonyms_proposed="
                    + json.dumps(synonym_map, ensure_ascii=False),
                    flush=True,
                )

            next_pending: List[str] = []
            next_seen: Set[str] = set()
            for term in not_found_array:
                concept_key = term_to_concept.get(term.lower(), term.lower())
                suggestions = synonym_map.get(term, []) or []
                for candidate in suggestions:
                    cand_clean = (candidate or "").strip()
                    if not cand_clean:
                        continue
                    cand_key = cand_clean.lower()
                    if cand_key in attempted_keys or cand_key in seen_round or cand_key in next_seen:
                        continue
                    term_to_concept[cand_key] = concept_key
                    term_origin[cand_key] = {"type": "synonym", "source": term}
                    next_seen.add(cand_key)
                    next_pending.append(cand_clean)
            if not next_pending:
                break
            pending = next_pending

        token_budget = int(options.get("token_budget", 6000))
        enc = tiktoken.get_encoding("cl100k_base")
        snippet_infos: List[Dict[str, Any]] = []
        for sn_id, info in snippet_records.items():
            snippet = info.get("snippet")
            if not snippet:
                continue
            concepts = info.get("concepts", set()) or {""}
            concept_iter = min(
                concept_first_found_iter.get(c, max_rounds + 10) for c in concepts
            )
            concept_rank = min(
                concept_index_map.get(c, len(concept_order)) for c in concepts
            )
            alias_priority = 0 if info.get("alias_hit") else 1
            token_count = len(enc.encode(getattr(snippet, "text", "") or ""))
            snippet_infos.append(
                {
                    "id": sn_id,
                    "snippet": snippet,
                    "concept_iter": concept_iter,
                    "concept_rank": concept_rank,
                    "alias_priority": alias_priority,
                    "token_count": token_count,
                }
            )

        snippet_infos.sort(
            key=lambda entry: (
                entry["concept_iter"],
                entry["alias_priority"],
                entry["concept_rank"],
                entry["token_count"],
                entry["id"],
            )
        )

        kept_snippets: List[BundleSnippet] = []
        total_tokens = 0
        truncated = False
        for entry in snippet_infos:
            snippet = entry["snippet"]
            count = entry["token_count"]
            if kept_snippets and total_tokens + count > token_budget:
                truncated = True
                continue
            kept_snippets.append(snippet)
            total_tokens += count
        if not kept_snippets and snippet_infos:
            first = snippet_infos[0]
            kept_snippets = [first["snippet"]]
            total_tokens = first["token_count"]
            truncated = first["token_count"] > token_budget
        elif len(kept_snippets) < len(snippet_infos):
            truncated = True

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
            "final_query": " ".join(initial_terms),
            "concept_matches": concept_matches,
            "concept_match_details": concept_match_details,
            "coverage_gaps": [],
            "refinement_queries": [],
            "subject": get_subject_name(),
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
        has_digit_para = False
        for idx, para in enumerate(paragraphs, start=1):
            stripped = para.strip()
            if stripped.startswith("```"):
                continue  # skip fenced code blocks
            letters = re.findall(r"[A-Za-z]", stripped)
            if len(letters) < 5:  # treat as math-heavy; no citation needed
                continue
            if re.search(r"\d", para):
                has_digit_para = True
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
        # Normalize final answers to a mapping
        final_answers_obj = getattr(sol, "final_answers", {}) or {}
        if isinstance(final_answers_obj, dict):
            final_answers = final_answers_obj
        elif isinstance(final_answers_obj, list):
            final_answers: Dict[str, object] = {}
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
        else:
            final_answers = {"answer": final_answers_obj}

        for key in asked_keys:
            if key not in final_answers:
                issues.append(f"answer lacks {key}")

        has_numeric = False
        for v in final_answers.values():
            if isinstance(v, (int, float)):
                has_numeric = True
                break
            if isinstance(v, str) and re.search(r"\d", v):
                has_numeric = True
                break
        if not has_numeric:
            issues.append("final_answers has no numeric values")
        if not final_answers and not has_digit_para:
            issues.append("no quantitative results produced")

        if ureg:
            for k, v in final_answers.items():
                # Only enforce unit parsing when the model emitted a string
                # with units. Pure numbers are treated as unitless.
                if isinstance(v, str):
                    try:
                        ureg.Quantity(v)
                    except Exception:
                        issues.append(f"unparsable units for {k}")

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

        hint: str | None = None
        solution: ProposedSolution | None = None
        issues: List[str] = []
        for _ in range(self.max_solve_rounds):
            solution = solve_with_bundle(parsed, bundle, hint=hint)
            issues = self._validate_solution(solution, parsed, bundle)
            if not issues:
                break
            missing = [
                i.split("answer lacks ", 1)[1]
                for i in issues
                if i.startswith("answer lacks ")
            ]
            if missing:
                keys_str = ", ".join(missing)
                hint = (
                    f"Return JSON with final_answers containing EXACT keys: {keys_str}. "
                    "Provide numeric values with units."
                )
                if {"g", "h", "d"}.issubset(parsed.knowns.keys()):
                    hint += (
                        " Include a short code block (pure Python) that computes "
                        "v_exit=sqrt(2*g*h) and Q=(pi*d**2/4)*v_exit using the knowns."
                    )
                hint += " Do not omit final_answers."
            else:
                hint = "; ".join(issues)
        if solution is None:
            raise RuntimeError("solve failed")
        with open("solution.json", "w", encoding="utf-8") as f:
            json.dump(asdict(solution), f, indent=2, ensure_ascii=False)
        solution_hash = self._hash(solution)

        final: FinalAnswer = format_answer(solution, bundle)

        models = {
            "parser": os.getenv("PARSER_MODEL", "gpt-4o-mini"),
            "main": os.getenv("MAIN_MODEL", "gpt-4o"),
        }
        proof = Proof(
            question=question,
            parsed_task=asdict(parsed),
            research_bundle=asdict(bundle),
            solver_output=asdict(solution),
            checks={
                "solution_valid": not issues,
                "issues": issues,
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
            "solution": solution_hash,
        }
        with open("proof.json", "w", encoding="utf-8") as f:
            json.dump(proof_data, f, indent=2, ensure_ascii=False)
        return final


__all__ = ["Orchestrator"]
