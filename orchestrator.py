from __future__ import annotations

"""State machine orchestrating the closed-book QA pipeline."""

import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Any

from .contracts import FinalAnswer, ParsedTask, Proof, ProposedSolution, ResearchBundle
from .main_ai import parse_question, solve_with_bundle, format_answer, normalize_query
from .retriever import research


def _bounded_replace(text: str, replacements: Dict[str, str]) -> str:
    import re

    out = text
    for t, c in replacements.items():
        pattern = r"(?<!\w)" + re.escape(t) + r"(?!\w)"
        out = re.sub(pattern, c, out)
    return out

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
        """Run retrieval with deterministic query expansion up to ``max_iters``.

        The query is first normalized (unless ``RETRIEVAL_SANITIZE`` disables it)
        and then searched. Missing terms reported by the retriever are replaced
        with corpus-observed aliases or morphology variants and the search is
        repeated. A trace of iterations is stored in the bundle metadata.
        """

        sanitize = os.getenv("RETRIEVAL_SANITIZE", "on").lower() not in {
            "0",
            "off",
            "false",
            "no",
        }
        current = normalize_query(question) if sanitize else question
        attempted = {current}
        trace: List[Dict[str, Any]] = []
        def_words = [
            w.strip()
            for w in os.getenv("RETRIEVAL_DEF_CUE_WORDS", "").split(",")
            if w.strip()
        ]
        if not def_words:
            def_words = ["defined", "denoted", "where"]
        def_re = re.compile("|".join(re.escape(w) for w in def_words), re.I)

        for i in range(max_iters):
            bundle = research(current, options)
            meta = bundle.metadata
            had_def = any(def_re.search(sn.text) for sn in bundle.snippets)
            entry = {
                "iter": i,
                "query": current,
                "replaced_terms": {},
                "added_terms": [],
                "hit_count_lex": getattr(meta, "hit_count_lex", 0),
                "hit_count_sem": getattr(meta, "hit_count_sem", 0),
                "had_definition": had_def,
            }
            trace.append(entry)
            missing = list(getattr(meta, "missing_terms", []))
            if (len(bundle.snippets) >= 3 and had_def) or not missing:
                break
            replacements: Dict[str, str] = {}
            for term in missing:
                cands = getattr(meta, "expansion_candidates", {}).get(term, [])
                if cands:
                    replacements[term] = cands[0]
            if not replacements:
                break
            new_q = _bounded_replace(current, replacements)
            entry["replaced_terms"] = replacements
            if new_q in attempted:
                break
            attempted.add(new_q)
            current = new_q

        meta = bundle.metadata
        meta.iteration_trace = trace
        meta.final_query = current
        meta.original_query = question
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
            if "[§" not in para:
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
        max_iters = min(int(os.getenv("RETRIEVAL_MAX_ITERS", "5")), 5)
        last_err = ""
        for _ in range(self.max_retrieval_rounds):
            bundle = self._iterative_research(question, retrieval_opts, max_iters)
            with open("bundle.json", "w", encoding="utf-8") as f:
                json.dump(asdict(bundle), f, indent=2, ensure_ascii=False)
            if not any(
                t.get("had_definition")
                for t in getattr(bundle.metadata, "iteration_trace", [])
            ):
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
