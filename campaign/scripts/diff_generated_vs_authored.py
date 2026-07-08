"""Pure graph-diff helpers for the authored calc-2 eval (reversed provisioning).

Aligns GENERATED authored-set problems (the upload path's promoted/held
``apollo_concept_problems`` payloads) to the authored corpus
(``apollo/provisioning/corpora/calc2/authored/authored_corpus.json``) by
problem-text token overlap, scores concept-match accuracy against the corpus's
private ``concept_slug`` ground truth, and diffs each generated reference
graph against its committed gold counterpart
(``apollo/subjects/calculus_2/.../problems/problem_*.json``).

Pure functions only — the driver (``eval_authored_calc2.py``) owns HTTP + SQL.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

__all__ = [
    "align_problems",
    "diff_graph",
    "norm_slug",
    "score_concept_match",
    "text_jaccard",
]

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_ALIGN_THRESHOLD = 0.6

# Meaningful snake_case id (interior uppercase symbol references allowed) with
# >=1 real word — mirrors graph_derivation._opaque_id_defect.
_ID_RE = re.compile(r"^[a-z][A-Za-z0-9_]*$")


def norm_slug(slug: str) -> str:
    return slug.strip().lower().replace("-", "_")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall((text or "").lower()))


def text_jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def align_problems(
    generated: Sequence[dict], corpus: Sequence[dict]
) -> list[tuple[dict, dict | None]]:
    """Greedy best-match alignment of generated problems to corpus entries by
    problem-text Jaccard (>= 0.6), each corpus entry used at most once."""
    used: set[int] = set()
    aligned: list[tuple[dict, dict | None]] = []
    for gen in generated:
        gen_text = str(
            (gen.get("payload") or {}).get("problem_text") or gen.get("problem_text") or ""
        )
        best_i, best_score = -1, 0.0
        for i, entry in enumerate(corpus):
            if i in used:
                continue
            score = text_jaccard(gen_text, str(entry.get("problem_text") or ""))
            if score > best_score:
                best_i, best_score = i, score
        if best_i >= 0 and best_score >= _ALIGN_THRESHOLD:
            used.add(best_i)
            aligned.append((gen, corpus[best_i]))
        else:
            aligned.append((gen, None))
    return aligned


def score_concept_match(aligned: Iterable[tuple[dict, dict | None]]) -> dict:
    """Concept-match accuracy over aligned problems.

    Each generated dict carries ``concept_slug`` (the concept it was minted
    into; for held problems the matcher's slug from provenance, or None for a
    NO_MATCH hold). Truth is the corpus entry's private ``concept_slug``."""
    total = correct = 0
    unaligned = 0
    no_match_held = 0
    per_concept: dict[str, dict[str, int]] = {}
    misses: list[dict] = []
    for gen, entry in aligned:
        if entry is None:
            unaligned += 1
            continue
        truth = norm_slug(str(entry.get("concept_slug") or ""))
        predicted_raw = gen.get("concept_slug")
        if predicted_raw is None:
            no_match_held += 1
            total += 1
            bucket = per_concept.setdefault(truth, {"correct": 0, "total": 0})
            bucket["total"] += 1
            misses.append(
                {
                    "problem_id": entry.get("problem_id"),
                    "truth": truth,
                    "predicted": None,
                    "outcome": gen.get("outcome"),
                }
            )
            continue
        predicted = norm_slug(str(predicted_raw))
        total += 1
        bucket = per_concept.setdefault(truth, {"correct": 0, "total": 0})
        bucket["total"] += 1
        if predicted == truth:
            correct += 1
            bucket["correct"] += 1
        else:
            misses.append(
                {
                    "problem_id": entry.get("problem_id"),
                    "truth": truth,
                    "predicted": predicted,
                    "outcome": gen.get("outcome"),
                }
            )
    return {
        "total": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "unaligned": unaligned,
        "no_match_held": no_match_held,
        "per_concept": per_concept,
        "misses": misses,
    }


def _steps(problem: dict) -> list[dict]:
    return list(problem.get("reference_solution") or [])


def _edge_pairs(problem: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for step in _steps(problem):
        sid = str(step.get("id"))
        for dep in step.get("depends_on") or []:
            pairs.append((sid, str(dep)))
    return sorted(pairs)


def _opaque_ids(problem: dict) -> list[str]:
    bad: list[str] = []
    for step in _steps(problem):
        sid = str(step.get("id"))
        tokens = [t for t in sid.split("_") if t]
        if not _ID_RE.match(sid) or not any(len(t) >= 3 and t.isalpha() for t in tokens):
            bad.append(sid)
    return bad


def _entry_type_histogram(problem: dict) -> dict[str, int]:
    hist: dict[str, int] = {}
    for step in _steps(problem):
        et = str(step.get("entry_type"))
        hist[et] = hist.get(et, 0) + 1
    return hist


def diff_graph(generated_payload: dict, committed_problem: dict | None) -> dict[str, Any]:
    """Structural comparison of one generated graph vs its gold counterpart."""
    gen_ids = {str(s.get("id")) for s in _steps(generated_payload)}
    out: dict[str, Any] = {
        "node_count": (
            len(_steps(generated_payload)),
            len(_steps(committed_problem)) if committed_problem else None,
        ),
        "entry_type_histogram": {
            "generated": _entry_type_histogram(generated_payload),
            "committed": _entry_type_histogram(committed_problem) if committed_problem else None,
        },
        "edge_pairs_generated": _edge_pairs(generated_payload),
        "edge_pairs_committed": _edge_pairs(committed_problem) if committed_problem else None,
        "opaque_ids": _opaque_ids(generated_payload),
    }
    if committed_problem is not None:
        committed_ids = {str(s.get("id")) for s in _steps(committed_problem)}
        out["shared_meaningful_ids"] = sorted(gen_ids & committed_ids)
        out["generated_only"] = sorted(gen_ids - committed_ids)
        out["committed_only"] = sorted(committed_ids - gen_ids)
    return out
