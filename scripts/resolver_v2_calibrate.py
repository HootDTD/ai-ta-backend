#!/usr/bin/env python
"""Resolver V2 DB-free calibration sweep on the frozen F1c gold ledgers (T8).

Design: docs/_archive/specs/2026-07-07-resolver-v2-design.md §9 / task card T8.

Reads (ALL read-only):
  * ``campaign/out/f1c/attempts.jsonl`` — frozen transcripts + per-persona
    ``expected.{credited,unresolved,misconceptions}`` gold ledgers;
  * ``apollo/subjects/**/problems/*.json`` + the linear_motion reference
    payload under ``campaign/cast/personas/linear_motion/reference/**`` (the
    same enumeration ``scripts/generate_resolver_v2_views.py`` uses);
  * the committed view cache (via ``apollo.resolver_v2.views.load_views``).

Gold labels per §9, per reference key on the union of declared paths:
  * ``k in expected.credited``    -> POSITIVE (persona provably taught it)
  * ``k in expected.unresolved``  -> NEGATIVE (deliberately omitted/mangled)
  * control personas (misconception / vague_then_clarifies): any path key
    not in ``expected.credited``  -> NEGATIVE (per-node control negatives)

Harness: pure V2 signal — real NLI (deberta-v3-large, CPU, HF_HUB_OFFLINE=1),
``grayzone_fn=None``, empty v1 floors, no DB, no Neo4j. All (node, window,
view) NLI pairs are scored ONCE per attempt and cached; every threshold combo
is post-hoc arithmetic over the cached (lexical, entailment, contradiction)
triples — NLI is never re-run per combination.

Objective (recall-first, R4): detection := credit >= 0.7 (== score >= t_mid).
Grid-search (t_low, t_mid, t_high, alpha, max_contradiction) per §9 plus a
small lex_floor axis; maximize node recall on positives subject to
false-credit rate <= 5% on negatives (all AND control-only). Tie-breaks:
lower FCR, higher mean positive margin (score_pos - t_mid), higher
strong-vs-misconception attempt-coverage separation, then combo order.

Also runs the §9 cross-subject held-out check (fit on macroeconomics ->
evaluate on fluid_mechanics, and vice versa) and one full-engine pass at the
winning params (edge-credit distribution per class — edge thresholds are NOT
swept tonight, no edge gold).

Usage (repo root; source .env.campaign first for HF_HOME):

    python scripts/resolver_v2_calibrate.py \
        --out campaign/out/resolver-v2/calibration-2026-07-07.json
    # reuse a previous run's NLI scores (sweep-only, no model load):
    python scripts/resolver_v2_calibrate.py --pair-cache <path> --out <path>
"""
from __future__ import annotations

import argparse
import datetime as _dt
import itertools
import json
import logging
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apollo.graph_compare.canonical import build_reference_canonical  # noqa: E402
from apollo.resolution.nli_adjudicator import (  # noqa: E402
    NLIResult,
    TransformersNLIAdjudicator,
    normalize_nli_output,
)
from apollo.resolution.polarity import polarity_allows_match  # noqa: E402
from apollo.resolver_v2.config import ResolverV2Params  # noqa: E402
from apollo.resolver_v2.engine import run_resolver_v2  # noqa: E402
from apollo.resolver_v2.prefilter import select_windows  # noqa: E402
from apollo.resolver_v2.views import build_ref_nodes, load_views  # noqa: E402
from apollo.resolver_v2.windows import build_windows  # noqa: E402

_LOG = logging.getLogger("resolver_v2_calibrate")

ATTEMPTS_PATH = REPO_ROOT / "campaign" / "out" / "f1c" / "attempts.jsonl"
DEFAULT_OUT = REPO_ROOT / "campaign" / "out" / "resolver-v2" / "calibration-2026-07-07.json"

# Same enumeration as scripts/generate_resolver_v2_views.py (T2): the 10
# committed seed problems + the auto-provisioned linear_motion reference
# payload that lives with the campaign cast (no apollo/subjects/ dir).
PROBLEM_GLOBS: tuple[str, ...] = (
    "apollo/subjects/*/concepts/*/problems/*.json",
    "campaign/cast/personas/linear_motion/reference/*/problems/*.json",
)

# Mirrors campaign.replay.CONTROL_PERSONAS (not imported: that module pulls
# the whole DB/grading stack; calibration is DB-free by design).
CONTROL_PERSONAS: frozenset[str] = frozenset({"misconception", "vague_then_clarifies"})

# §9 detection bar: credit >= 0.7  <=>  fused score >= t_mid (grayzone OFF,
# v1 floors OFF, and the edge pull-up floor 0.6 < 0.7 cannot flip detection).
DETECTION_CREDIT: float = 0.7
FCR_CEILING: float = 0.05

# §9 grid + a small lex_floor axis (the skip rule is post-hoc arithmetic too;
# the design default 0.10 is included so "no change" is representable).
GRID: dict[str, tuple[float, ...]] = {
    "t_high": (0.80, 0.85, 0.90, 0.95),
    "t_mid": (0.60, 0.70, 0.75),
    "t_low": (0.30, 0.40, 0.50),
    "alpha": (0.75, 0.85, 1.0),
    "max_contradiction": (0.20, 0.30, 0.50),
    "lex_floor": (0.05, 0.10, 0.15),
}

_NLI_BATCH_SIZE = 16


# ---------------------------------------------------------------------------
# Gold-label derivation (§9) — pure, unit-tested
# ---------------------------------------------------------------------------


def derive_labels(
    record: dict[str, Any], path_keys: frozenset[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """Per-node gold labels for one attempt record, per the §9 rules.

    Returns ``(positives, negatives)`` restricted to ``path_keys`` (the union
    of the reference graph's declared paths — off-path expected keys are
    dropped). Control personas (misconception / vague_then_clarifies) add
    every path key NOT in ``expected.credited`` as a negative (per-node
    control negatives — misconception personas teach 4/5 beats correctly).
    A key can never be both: credited wins (positives take precedence).
    """
    expected = record.get("expected") or {}
    credited = {str(k) for k in expected.get("credited") or []}
    unresolved = {str(k) for k in expected.get("unresolved") or []}
    positives = frozenset(credited & path_keys)
    negatives = set(unresolved & path_keys)
    if str(record.get("persona", "")) in CONTROL_PERSONAS:
        negatives |= path_keys - credited
    negatives -= positives
    return positives, frozenset(negatives)


def student_turns_of(record: dict[str, Any]) -> tuple[str, ...]:
    """Student-turn texts from the record's recorded transcript, in order
    (mirrors ``integration.load_student_turns``'s role filter)."""
    transcript = record.get("transcript") or []
    return tuple(
        str(m.get("content", ""))
        for m in transcript
        if isinstance(m, dict) and m.get("role") == "student" and m.get("content")
    )


def load_calibration_records(attempts_path: Path) -> list[dict[str, Any]]:
    """Every F1c record with at least one student turn and an expected ledger
    (READ-only). Unlike the replay (which needs a DB attempt row), the DB-free
    sweep can also use the 5 ``status="error"`` records — their transcripts
    are truncated (the persona never finished teaching), which depresses
    measured recall by a combo-independent constant and can only make the
    false-credit constraint STRICTER; each record carries its ``status`` so
    the report can split them out.
    """
    records: list[dict[str, Any]] = []
    with attempts_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if not (record.get("expected") or {}).get("credited"):
                continue
            if not student_turns_of(record):
                continue
            records.append(record)
    return records


def load_problem_index() -> dict[str, dict[str, Any]]:
    """``{payload id: problem payload}`` over the same problem enumeration the
    view generator uses (READ-only; the dirty problem_02-05 diffs are read
    from the working tree but never written)."""
    index: dict[str, dict[str, Any]] = {}
    for pattern in PROBLEM_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            payload = json.loads(path.read_text(encoding="utf-8"))
            pid = payload.get("id")
            if pid:
                index[str(pid)] = payload
    return index


# ---------------------------------------------------------------------------
# One-time NLI pair scoring (the expensive pass — cached, never re-run)
# ---------------------------------------------------------------------------


class MemoNLI:
    """Memoizing wrapper over a real adjudicator with an optional batched
    fill path. ``classify`` serves cache hits for free; the engine
    verification pass reuses the same memo so only NEW pairs (verbalized
    edges) hit the model again."""

    def __init__(self, inner: TransformersNLIAdjudicator) -> None:
        self._inner = inner
        self.memo: dict[tuple[str, str], NLIResult] = {}
        self.classified = 0

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        key = (premise, hypothesis)
        cached = self.memo.get(key)
        if cached is None:
            cached = self._inner.classify(premise, hypothesis)
            self.memo[key] = cached
            self.classified += 1
        return cached

    def batch_fill(self, pairs: list[tuple[str, str]]) -> int:
        """Score every uncached (premise, hypothesis) pair, batched through
        the transformers pipeline when available (design §8: one batched
        call). Returns the number of REAL classifications run."""
        todo: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pair in pairs:
            if pair not in self.memo and pair not in seen:
                seen.add(pair)
                todo.append(pair)
        if not todo:
            return 0
        pipe = getattr(self._inner, "_load", None)
        if pipe is None:  # pragma: no cover - non-transformers adjudicator
            for premise, hypothesis in todo:
                self.classify(premise, hypothesis)
            return len(todo)
        pipeline = self._inner._load()  # noqa: SLF001 - deliberate batch access
        for start in range(0, len(todo), _NLI_BATCH_SIZE):
            chunk = todo[start : start + _NLI_BATCH_SIZE]
            raw_batch = pipeline(
                [{"text": p, "text_pair": h} for p, h in chunk],
                truncation=True,
                batch_size=_NLI_BATCH_SIZE,
            )
            for (premise, hypothesis), raw in zip(chunk, raw_batch):
                self.memo[(premise, hypothesis)] = normalize_nli_output(
                    raw, self._inner.model_name
                )
        self.classified += len(todo)
        return len(todo)


def _fallback_ref_views(
    payload: dict[str, Any], views_by_key: dict[str, tuple[str, ...]]
) -> tuple[list[list[str]], dict[str, tuple[str, ...]]]:
    """Label+views fallback when ``build_reference_canonical`` rejects the
    payload (the F1c linear_motion reference file has no ``declared_paths`` —
    it was auto-provisioned into the DB; the committed file carries only the
    reference steps, exactly what ``generate_resolver_v2_views.read_problem``
    reads). Returns ``(paths, {key: views})`` with ONE pseudo-path = the
    reference steps in order, and views mirroring ``build_ref_nodes``
    (label prepended, dedup order-preserving)."""
    steps = payload.get("reference_solution") or []
    keys: list[str] = []
    views: dict[str, tuple[str, ...]] = {}
    for step in steps:
        key = step.get("entity_key")
        if not isinstance(key, str) or key in views:
            continue
        content = step.get("content") or {}
        label = content.get("label") if isinstance(content, dict) else None
        label = label.strip() if isinstance(label, str) and label.strip() else key
        seen = {label}
        extra = [v for v in views_by_key.get(key, ()) if not (v in seen or seen.add(v))]
        keys.append(key)
        views[key] = (label, *extra)
    return [keys], views


def build_attempt_case(
    record: dict[str, Any], payload: dict[str, Any], base_params: ResolverV2Params
) -> dict[str, Any] | None:
    """Deterministic (NLI-free) half of one attempt's cache entry: windows,
    ref nodes with views, selected (window, view) pairs with lexical scores +
    polarity verdicts, and the §9 gold labels. A payload whose reference graph
    fails validation degrades to the reference-steps fallback (linear_motion);
    a payload with no usable reference at all returns ``None`` (logged)."""
    problem_id = str(record["problem_id"])
    turns = student_turns_of(record)
    windows = build_windows(turns, base_params)
    views_by_key = load_views(str(payload.get("concept_id") or ""), problem_id)
    reference_fallback = False
    try:
        reference_graph = build_reference_canonical(payload)
        ref_nodes = build_ref_nodes(reference_graph, payload, views_by_key)
        paths = [list(p.canonical_keys) for p in reference_graph.paths]
        views_of = {n.canonical_key: n.views for n in ref_nodes}
    except Exception as exc:  # noqa: BLE001 - degrade to the steps fallback
        _LOG.warning(
            "reference_graph_invalid_using_steps_fallback problem=%s error=%s",
            problem_id,
            exc,
        )
        paths, views_of = _fallback_ref_views(payload, views_by_key)
        reference_fallback = True
        if not views_of:
            _LOG.warning("no_reference_steps problem=%s", problem_id)
            return None
    path_keys = frozenset(k for path in paths for k in path)
    positives, negatives = derive_labels(record, path_keys)
    window_text = {w.index: w.text for w in windows}

    nodes: list[dict[str, Any]] = []
    for key in sorted(views_of):
        pairs: list[dict[str, Any]] = []
        for view_index, view_text in enumerate(views_of[key]):
            for win_index, lex in select_windows(windows, view_text, base_params.top_k_windows):
                allowed = polarity_allows_match(window_text[win_index], view_text).allowed
                pairs.append(
                    {
                        "view_index": view_index,
                        "window_index": win_index,
                        "lex": lex,
                        "allowed": allowed,
                        "premise": window_text[win_index],
                        "hypothesis": view_text,
                        "entailment": 0.0,
                        "contradiction": 0.0,
                    }
                )
        nodes.append(
            {
                "key": key,
                "max_lex": max((p["lex"] for p in pairs), default=0.0),
                "pairs": pairs,
            }
        )
    return {
        "case_id": f"{problem_id}/{record.get('persona')}",
        "attempt_id": record.get("attempt_id"),
        "status": record.get("status"),
        "subject": record.get("subject"),
        "persona": record.get("persona"),
        "problem_id": problem_id,
        "is_control": str(record.get("persona", "")) in CONTROL_PERSONAS,
        "reference_fallback": reference_fallback,
        "paths": paths,
        "positives": sorted(positives),
        "negatives": sorted(negatives),
        "n_windows": len(windows),
        "nodes": nodes,
    }


def fill_case_nli(case: dict[str, Any], nli: MemoNLI) -> int:
    """Run the one-time NLI scoring for one attempt case, batched. Only
    polarity-allowed pairs are classified (mirrors ``scoring.score_nodes`` —
    disallowed pairs contribute 0 and never reach the model)."""
    wanted = [
        (p["premise"], p["hypothesis"])
        for node in case["nodes"]
        for p in node["pairs"]
        if p["allowed"]
    ]
    ran = nli.batch_fill(wanted)
    for node in case["nodes"]:
        for pair in node["pairs"]:
            if pair["allowed"]:
                result = nli.memo[(pair["premise"], pair["hypothesis"])]
                pair["entailment"] = float(result.entailment)
                pair["contradiction"] = float(result.contradiction)
    return ran


def strip_texts(case: dict[str, Any]) -> dict[str, Any]:
    """Drop premise/hypothesis strings for the persisted pair cache (scores
    only — the texts are reproducible from the frozen corpus + view cache)."""
    slim = dict(case)
    slim["nodes"] = [
        {
            "key": node["key"],
            "max_lex": node["max_lex"],
            "pairs": [
                {k: v for k, v in pair.items() if k not in ("premise", "hypothesis")}
                for pair in node["pairs"]
            ],
        }
        for node in case["nodes"]
    ]
    return slim


# ---------------------------------------------------------------------------
# Post-hoc combo evaluation (pure arithmetic over the cached scores)
# ---------------------------------------------------------------------------


def node_score_for(node: dict[str, Any], combo: dict[str, float]) -> float:
    """Mirror of ``scoring.score_nodes``'s per-node outcome ladder under one
    threshold combo (grayzone OFF, v1 floors OFF, budget never binding on
    this corpus — max ~120 selected pairs/attempt vs the 200 cap):

    * no pairs -> 0.0;
    * max lexical < lex_floor -> the max lexical score (skip rule §5.3);
    * else max over pairs of fused, where a polarity-disallowed pair or one
      with contradiction > max_contradiction contributes 0.
    """
    if not node["pairs"]:
        return 0.0
    if node["max_lex"] < combo["lex_floor"]:
        return node["max_lex"]
    alpha = combo["alpha"]
    max_con = combo["max_contradiction"]
    best = 0.0
    for pair in node["pairs"]:
        if not pair["allowed"] or pair["contradiction"] > max_con:
            continue
        fused = alpha * pair["entailment"] + (1.0 - alpha) * pair["lex"]
        if fused > best:
            best = fused
    return best


def credit_of(score: float, combo: dict[str, float]) -> float:
    """§5.5 graded credit, grayzone OFF (gray band -> 0.3)."""
    if score >= combo["t_high"]:
        return 1.0
    if score >= combo["t_mid"]:
        return 0.7
    if score >= combo["t_low"]:
        return 0.3
    return 0.0


def evaluate_combo(
    cases: list[dict[str, Any]], combo: dict[str, float]
) -> dict[str, Any]:
    """Recall / false-credit metrics for one combo over ``cases``.

    detection := credit >= 0.7  <=>  node score >= t_mid. Also computes the
    graded winning-path node coverage per attempt (the discrimination
    tie-break: strong-class mean minus misconception-class mean).
    """
    pos_total = pos_hit = 0
    neg_total = neg_hit = 0
    ctrl_neg_total = ctrl_neg_hit = 0
    margins: list[float] = []
    coverage_by_class: dict[str, list[float]] = {}

    for case in cases:
        scores = {node["key"]: node_score_for(node, combo) for node in case["nodes"]}
        for key in case["positives"]:
            score = scores.get(key, 0.0)
            pos_total += 1
            margins.append(score - combo["t_mid"])
            if score >= combo["t_mid"]:
                pos_hit += 1
        for key in case["negatives"]:
            score = scores.get(key, 0.0)
            neg_total += 1
            detected = score >= combo["t_mid"]
            if detected:
                neg_hit += 1
            if case["is_control"]:
                ctrl_neg_total += 1
                if detected:
                    ctrl_neg_hit += 1
        credits = {k: credit_of(s, combo) for k, s in scores.items()}
        coverage = max(
            (
                sum(credits.get(k, 0.0) for k in path) / len(path)
                for path in case["paths"]
                if path
            ),
            default=0.0,
        )
        coverage_by_class.setdefault(str(case["persona"]), []).append(coverage)

    strong = coverage_by_class.get("strong") or [0.0]
    misc = coverage_by_class.get("misconception") or [0.0]
    return {
        "recall": pos_hit / pos_total if pos_total else 0.0,
        "fcr": neg_hit / neg_total if neg_total else 0.0,
        "fcr_control": ctrl_neg_hit / ctrl_neg_total if ctrl_neg_total else 0.0,
        "n_pos": pos_total,
        "n_neg": neg_total,
        "n_neg_control": ctrl_neg_total,
        "mean_margin": statistics.fmean(margins) if margins else 0.0,
        "separation": statistics.fmean(strong) - statistics.fmean(misc),
        "mean_coverage_by_class": {
            cls: round(statistics.fmean(vals), 4) for cls, vals in sorted(coverage_by_class.items())
        },
    }


def all_combos() -> list[dict[str, float]]:
    """The full grid, deterministic order, invalid orderings excluded
    (t_low < t_mid < t_high always holds on this grid but is asserted)."""
    names = list(GRID)
    combos: list[dict[str, float]] = []
    for values in itertools.product(*(GRID[n] for n in names)):
        combo = dict(zip(names, values))
        if combo["t_low"] < combo["t_mid"] < combo["t_high"]:
            combos.append(combo)
    return combos


# Design §7 pre-calibration defaults — the "don't move a knob the data is
# indifferent about" anchor for tie-breaking (see pick_winner).
_DESIGN_DEFAULTS: dict[str, float] = {
    "t_low": 0.40,
    "t_mid": 0.75,
    "t_high": 0.90,
    "alpha": 0.85,
    "max_contradiction": 0.30,
    "lex_floor": 0.10,
}


def _defaults_distance(combo: dict[str, float]) -> float:
    return sum(abs(combo[k] - v) for k, v in _DESIGN_DEFAULTS.items())


def pick_winner(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    """Recall-first argmax under the FCR ceilings (§9). Returns
    ``(winning row, constraint_satisfied)``.

    When NO combo meets both ceilings (measured on this corpus the ceilings
    are infeasible at EVERY grid point — the per-node gold negatives are
    noisy: the LLM personas teach most "omitted" content inline; see the
    ``negative_audit`` block of the output), the documented fallback policy
    is: maximize recall, then minimize FCR, then maximize the
    strong-vs-misconception coverage separation (the replay discrimination
    gate's analogue), then take the combo closest to the design §7 defaults
    (never move a knob the data is indifferent about), then lowest index.
    ``constraint_satisfied`` stays False — the report must show it.
    """
    feasible = [
        r
        for r in rows
        if r["metrics"]["fcr"] <= FCR_CEILING and r["metrics"]["fcr_control"] <= FCR_CEILING
    ]
    if feasible:
        best = max(
            feasible,
            key=lambda r: (
                r["metrics"]["recall"],
                -r["metrics"]["fcr"],
                r["metrics"]["mean_margin"],
                r["metrics"]["separation"],
                -_defaults_distance(r["combo"]),
                -r["index"],
            ),
        )
        return best, True
    best = max(
        rows,
        key=lambda r: (
            r["metrics"]["recall"],
            -r["metrics"]["fcr"],
            r["metrics"]["separation"],
            -_defaults_distance(r["combo"]),
            -r["index"],
        ),
    )
    return best, False


def negative_audit(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best entailment evidence for EVERY gold negative — the committed
    label-noise audit. A high-entailment 'negative' whose window verbatim
    teaches the content is a gold-ledger defect (the persona script omitted
    the beat but the LLM student taught it inline), not a resolver false
    credit; the report adjudicates each row."""
    audit: list[dict[str, Any]] = []
    for case in cases:
        by_key = {node["key"]: node for node in case["nodes"]}
        for key in case["negatives"]:
            node = by_key.get(key)
            if node is None:
                continue
            allowed = [p for p in node["pairs"] if p["allowed"]]
            best = max(allowed, key=lambda p: p["entailment"], default=None)
            audit.append(
                {
                    "case_id": case["case_id"],
                    "persona": case["persona"],
                    "is_control": case["is_control"],
                    "key": key,
                    "entailment": round(best["entailment"], 4) if best else None,
                    "contradiction": round(best["contradiction"], 4) if best else None,
                    "lex": round(best["lex"], 4) if best else None,
                    "hypothesis": (best["hypothesis"][:160] if best else None),
                    "window": (
                        best["premise"][:280].replace("\n", " ") if best else None
                    ),
                }
            )
    return sorted(audit, key=lambda r: -(r["entailment"] or 0.0))


# ---------------------------------------------------------------------------
# Engine verification pass (winner params, real engine, edges included)
# ---------------------------------------------------------------------------


def engine_pass(
    cases: list[dict[str, Any]],
    records_by_case: dict[str, dict[str, Any]],
    payload_index: dict[str, dict[str, Any]],
    nli: MemoNLI,
    params: ResolverV2Params,
) -> dict[str, Any]:
    """Run the REAL engine once per attempt at the winning params (pure V2:
    empty v1 floors, grayzone off). Yields the edge-credit distribution per
    class (edge thresholds are not swept — no edge gold) and a consistency
    check of the sweep's post-hoc node scores against the engine's."""
    per_class_edges: dict[str, list[float]] = {}
    per_attempt: list[dict[str, Any]] = []
    evidence_counts: dict[str, int] = {}
    max_node_score_diff = 0.0
    combo = {
        "t_low": params.t_low,
        "t_mid": params.t_mid,
        "t_high": params.t_high,
        "alpha": params.alpha,
        "max_contradiction": params.max_contradiction,
        "lex_floor": params.lex_floor,
    }
    for case in cases:
        if case.get("reference_fallback"):
            continue  # no valid ReferenceGraph (linear_motion) — sweep-only case
        record = records_by_case[case["case_id"]]
        payload = payload_index[case["problem_id"]]
        result = run_resolver_v2(
            student_turns=student_turns_of(record),
            reference_graph=build_reference_canonical(payload),
            problem_payload=payload,
            v1_resolved_keys=frozenset(),
            v1_explicit_triples=frozenset(),
            v1_inferred_triples=frozenset(),
            nli=nli,
            grayzone_fn=None,
            params=params,
        )
        posthoc = {node["key"]: node_score_for(node, combo) for node in case["nodes"]}
        for node in result.node_scores:
            diff = abs(node.score - posthoc.get(node.canonical_key, 0.0))
            max_node_score_diff = max(max_node_score_diff, diff)
        for edge in result.edge_scores:
            evidence_counts[edge.relation_evidence] = (
                evidence_counts.get(edge.relation_evidence, 0) + 1
            )
        per_class_edges.setdefault(str(case["persona"]), []).append(result.edge_coverage)
        per_attempt.append(
            {
                "case_id": case["case_id"],
                "persona": case["persona"],
                "subject": case["subject"],
                "status": case["status"],
                "node_coverage": round(result.node_coverage, 4),
                "edge_coverage": round(result.edge_coverage, 4),
                "pair_count": result.pair_count,
            }
        )
    return {
        "edge_coverage_by_class": {
            cls: {
                "mean": round(statistics.fmean(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "n": len(vals),
            }
            for cls, vals in sorted(per_class_edges.items())
        },
        "edge_evidence_counts": dict(sorted(evidence_counts.items())),
        "per_attempt": per_attempt,
        "max_node_score_diff_vs_sweep": max_node_score_diff,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _sweep(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    combos = all_combos()
    rows: list[dict[str, Any]] = []
    for index, combo in enumerate(combos):
        rows.append({"index": index, "combo": combo, "metrics": evaluate_combo(cases, combo)})
    return rows


def _subset(cases: list[dict[str, Any]], subjects: set[str]) -> list[dict[str, Any]]:
    return [c for c in cases if c["subject"] in subjects]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--attempts", default=str(ATTEMPTS_PATH))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--pair-cache",
        default=None,
        help="reuse a previous run's scored pair cache JSON (skip the NLI pass)",
    )
    parser.add_argument(
        "--pair-cache-out",
        default=None,
        help="where to write the scored pair cache (default: <out dir>/nli-pair-scores.json)",
    )
    parser.add_argument("--probe-attempts", type=int, default=2)
    parser.add_argument("--skip-engine-check", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base_params = ResolverV2Params()  # design defaults for windows/top_k

    records = load_calibration_records(Path(args.attempts))
    payload_index = load_problem_index()
    print(f"calibration records: {len(records)} (incl. status=error truncated transcripts)")

    cases: list[dict[str, Any]] = []
    records_by_case: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for record in records:
        payload = payload_index.get(str(record["problem_id"]))
        if payload is None:
            skipped.append(f"{record['problem_id']}: no problem payload found")
            continue
        case = build_attempt_case(record, payload, base_params)
        if case is None:
            skipped.append(f"{record['problem_id']}: reference graph invalid")
            continue
        cases.append(case)
        records_by_case[case["case_id"]] = record

    timing: dict[str, Any] = {}
    nli: MemoNLI | None = None
    if args.pair_cache:
        cached = json.loads(Path(args.pair_cache).read_text(encoding="utf-8"))
        by_id = {c["case_id"]: c for c in cached["cases"]}
        for case in cases:
            saved = by_id[case["case_id"]]
            for node, saved_node in zip(case["nodes"], saved["nodes"]):
                for pair, saved_pair in zip(node["pairs"], saved_node["pairs"]):
                    pair["entailment"] = saved_pair["entailment"]
                    pair["contradiction"] = saved_pair["contradiction"]
        timing["source"] = f"pair cache reused: {args.pair_cache}"
        if not args.skip_engine_check:
            # Prefill a MemoNLI so the engine pass re-classifies ONLY new
            # pairs (verbalized edges) — node pairs replay from the cache.
            from apollo.resolution.nli_config import NLI_DEVICE, active_nli_model

            nli = MemoNLI(TransformersNLIAdjudicator(active_nli_model(), device=NLI_DEVICE))
            for case in cases:
                for node in case["nodes"]:
                    for pair in node["pairs"]:
                        if not pair["allowed"]:
                            continue
                        ent, con = pair["entailment"], pair["contradiction"]
                        neu = max(0.0, 1.0 - ent - con)
                        label = max(
                            ("entailment", ent), ("neutral", neu), ("contradiction", con),
                            key=lambda kv: kv[1],
                        )[0]
                        nli.memo[(pair["premise"], pair["hypothesis"])] = NLIResult(
                            label, ent, con, neu, "pair-cache"
                        )
    else:
        from apollo.resolution.nli_config import NLI_DEVICE, active_nli_model

        inner = TransformersNLIAdjudicator(active_nli_model(), device=NLI_DEVICE)
        nli = MemoNLI(inner)
        warm_start = time.perf_counter()
        inner.classify("water flows through a pipe", "fluid moves in a tube")
        timing["model_warmup_s"] = round(time.perf_counter() - warm_start, 2)

        # Card step 3: 2-attempt timing probe BEFORE the full pass.
        probe_n = max(0, min(args.probe_attempts, len(cases)))
        probe_start = time.perf_counter()
        probe_pairs = sum(fill_case_nli(case, nli) for case in cases[:probe_n])
        probe_wall = time.perf_counter() - probe_start
        pairs_per_s = probe_pairs / probe_wall if probe_wall > 0 else 0.0
        timing["probe"] = {
            "attempts": probe_n,
            "nli_pairs": probe_pairs,
            "wall_s": round(probe_wall, 2),
            "pairs_per_s": round(pairs_per_s, 2),
            "s_per_attempt": round(probe_wall / probe_n, 2) if probe_n else None,
        }
        print(
            f"timing probe: {probe_pairs} pairs / {probe_wall:.1f}s = "
            f"{pairs_per_s:.2f} pairs/s ({timing['probe']['s_per_attempt']}s/attempt)"
        )

        full_start = time.perf_counter()
        for case in cases[probe_n:]:
            fill_case_nli(case, nli)
        timing["full_pass"] = {
            "attempts": len(cases),
            "nli_pairs_total": nli.classified,
            "wall_s": round(time.perf_counter() - probe_start, 2),
        }
        print(
            f"NLI pass done: {nli.classified} classifications over {len(cases)} attempts "
            f"in {timing['full_pass']['wall_s']}s"
        )
        pair_cache_out = Path(args.pair_cache_out or out_path.parent / "nli-pair-scores.json")
        pair_cache_out.write_text(
            json.dumps(
                {"date": _dt.date.today().isoformat(), "cases": [strip_texts(c) for c in cases]},
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"pair cache -> {pair_cache_out}")

    # ---- full-corpus sweep -------------------------------------------------
    sweep_start = time.perf_counter()
    rows = _sweep(cases)
    winner, feasible = pick_winner(rows)
    timing["sweep_s"] = round(time.perf_counter() - sweep_start, 2)

    # ---- cross-subject held-out (the generalization evidence) --------------
    macro = _subset(cases, {"macroeconomics"})
    fluids = _subset(cases, {"fluid_mechanics"})
    linear = _subset(cases, {"linear_motion"})
    held_out: dict[str, Any] = {}
    for name, fit_cases, eval_cases in (
        ("fit_macro_eval_fluids", macro, fluids),
        ("fit_fluids_eval_macro", fluids, macro),
    ):
        fit_rows = [
            {"index": i, "combo": r["combo"], "metrics": evaluate_combo(fit_cases, r["combo"])}
            for i, r in enumerate(rows)
        ]
        fit_winner, fit_feasible = pick_winner(fit_rows)
        held_out[name] = {
            "fit_combo": fit_winner["combo"],
            "fit_feasible": fit_feasible,
            "fit_metrics": fit_winner["metrics"],
            "eval_metrics": evaluate_combo(eval_cases, fit_winner["combo"]),
            "eval_linear_motion": evaluate_combo(linear, fit_winner["combo"]) if linear else None,
            "full_winner_on_eval_subset": evaluate_combo(eval_cases, winner["combo"]),
        }

    # ---- engine verification at the winner (edges, no edge-gold sweep) -----
    engine_check: dict[str, Any] | None = None
    if not args.skip_engine_check and nli is not None:
        winner_params = replace(
            base_params,
            t_low=winner["combo"]["t_low"],
            t_mid=winner["combo"]["t_mid"],
            t_high=winner["combo"]["t_high"],
            alpha=winner["combo"]["alpha"],
            max_contradiction=winner["combo"]["max_contradiction"],
            lex_floor=winner["combo"]["lex_floor"],
        )
        engine_start = time.perf_counter()
        engine_check = engine_pass(cases, records_by_case, payload_index, nli, winner_params)
        engine_check["wall_s"] = round(time.perf_counter() - engine_start, 2)

    ok_cases = [c for c in cases if c["status"] == "ok"]
    output = {
        "date": _dt.date.today().isoformat(),
        "script": "scripts/resolver_v2_calibrate.py",
        "design": "docs/_archive/specs/2026-07-07-resolver-v2-design.md §9 / T8",
        "corpus": {
            "attempts_path": str(Path(args.attempts)),
            "cases": len(cases),
            "cases_ok": len(ok_cases),
            "cases_error_status": len(cases) - len(ok_cases),
            "by_subject": {
                s: sum(1 for c in cases if c["subject"] == s)
                for s in sorted({c["subject"] for c in cases})
            },
            "by_persona": {
                p: sum(1 for c in cases if c["persona"] == p)
                for p in sorted({c["persona"] for c in cases})
            },
            "n_pos": sum(len(c["positives"]) for c in cases),
            "n_neg": sum(len(c["negatives"]) for c in cases),
            "skipped": skipped,
        },
        "objective": {
            "detection": f"node credit >= {DETECTION_CREDIT} (score >= t_mid)",
            "maximize": "node recall on positives",
            "subject_to": f"FCR <= {FCR_CEILING} on ALL negatives AND control-only negatives",
            "tie_breaks": [
                "lower FCR",
                "higher mean positive margin (score - t_mid)",
                "higher strong-minus-misconception mean coverage separation",
                "lowest combo index (deterministic)",
            ],
        },
        "grid": {k: list(v) for k, v in GRID.items()},
        "n_combos": len(rows),
        "timing": timing,
        "winner": {
            "combo": winner["combo"],
            "constraint_satisfied": feasible,
            "policy": (
                "recall_first_fcr_constrained"
                if feasible
                else "infeasible_fallback: max recall -> min fcr -> max separation "
                "-> nearest design defaults (see pick_winner docstring)"
            ),
            "metrics": winner["metrics"],
        },
        "negative_audit": negative_audit(cases),
        "held_out": held_out,
        "engine_check": engine_check,
        "sweep": sorted(
            (
                {"combo": r["combo"], "metrics": r["metrics"]}
                for r in rows
            ),
            key=lambda r: (-r["metrics"]["recall"], r["metrics"]["fcr"]),
        ),
    }
    out_path.write_text(json.dumps(output, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwinner (feasible={feasible}): {winner['combo']}")
    print(f"in-sample: {json.dumps(winner['metrics'], sort_keys=True)}")
    for name, block in held_out.items():
        print(f"{name}: fit={block['fit_combo']} eval={json.dumps(block['eval_metrics'])}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
