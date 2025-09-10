from __future__ import annotations

"""State machine orchestrating the closed-book QA pipeline."""

import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List

from .contracts import FinalAnswer, ParsedTask, Proof, ProposedSolution, ResearchBundle
from .main_ai import parse_question, solve_with_bundle, format_answer
from .retriever import research
from .solver import ureg


class Orchestrator:
    """Sequential orchestrator with validation and retries."""

    def __init__(self, max_retrieval_rounds: int = 2, max_solve_rounds: int = 2):
        self.max_retrieval_rounds = max_retrieval_rounds
        self.max_solve_rounds = max_solve_rounds

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
        paragraphs = [p for p in sol.steps.split("\n") if p.strip()]
        for idx, para in enumerate(paragraphs, start=1):
            stripped = para.strip()
            if stripped.startswith("```"):
                continue  # skip fenced code blocks
            letters = re.findall(r"[A-Za-z]", stripped)
            if len(letters) < 5:  # treat as math-heavy; no citation needed
                continue
            if "[§" not in para:
                issues.append(f"missing citation in paragraph {idx}")
        asked_raw = getattr(parsed, "asked_outputs", [])
        if isinstance(asked_raw, list):
            asked_list = asked_raw
        elif isinstance(asked_raw, str) and asked_raw.strip():
            asked_list = [asked_raw]
        else:
            asked_list = []
        for key in asked_list:
            if key not in sol.final_answers:
                issues.append(f"answer lacks {key}")
        if ureg:
            for k, v in sol.final_answers.items():
                # Only enforce unit parsing when the model emitted a string
                # with units. Pure numbers are treated as unitless.
                if isinstance(v, str):
                    try:
                        ureg.Quantity(v)
                    except Exception:
                        issues.append(f"unparsable units for {k}")
        bundle_eqs = {self._normalize_eq(e["eq_text"]) for e in bundle.equations}
        for eq in sol.equations_used:
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
            bundle = research(parsed, retrieval_opts)
            try:
                bundle.validate()
                break
            except Exception as exc:
                last_err = str(exc)
                retrieval_opts["token_budget"] *= 2
                retrieval_opts["k_sem"] = min(retrieval_opts.get("k_sem", 30) * 2, 100)
                retrieval_opts["k_lex"] = min(retrieval_opts.get("k_lex", 30) * 2, 100)
        else:
            raise RuntimeError(f"insufficient book context: {last_err}")
        if bundle is None:
            raise RuntimeError("retrieval failed")
        with open("bundle.json", "w", encoding="utf-8") as f:
            json.dump(asdict(bundle), f, indent=2, ensure_ascii=False)
        bundle_hash = self._hash(bundle)

        hint: str | None = None
        solution: ProposedSolution | None = None
        issues: List[str] = []
        for _ in range(self.max_solve_rounds):
            solution = solve_with_bundle(parsed, bundle, hint=hint)
            issues = self._validate_solution(solution, parsed, bundle)
            if not issues:
                break
            hint = "; ".join(issues)
        if solution is None:
            raise RuntimeError("solve failed")
        with open("solution.json", "w", encoding="utf-8") as f:
            json.dump(asdict(solution), f, indent=2, ensure_ascii=False)
        solution_hash = self._hash(solution)

        final: FinalAnswer = format_answer(solution, bundle)

        models = {
            "parser": os.getenv("PARSER_MODEL", "gpt-4o-mini"),
            "main": os.getenv("MAIN_MODEL", "gpt-5o"),
        }
        proof = Proof(
            question=question,
            parsed_task=asdict(parsed),
            research_bundle=asdict(bundle),
            solver_output=asdict(solution),
            checks={
                "solution_valid": not issues,
                "issues": issues,
                "skipped_indexes": bundle.metadata.get("skipped_indexes", []),
                "models": models,
                "retrieval_params": {
                    "k_sem": bundle.metadata.get("k_sem"),
                    "k_lex": bundle.metadata.get("k_lex"),
                    "token_budget": bundle.metadata.get("token_budget"),
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
