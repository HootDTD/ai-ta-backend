"""Pure edge-contraction eligibility with symbolic safeguards (DAG-5).

Concrete equation intermediates use bridge equations as a symbolic confirm/veto
channel. Simplifications, display equations, and prose fail closed because no
deterministic equivalence proof is available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import sympy

from apollo.graph_compare.canonical import (
    CanonicalGraph,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)
from apollo.graph_compare.findings import FindingKind

_COMBINABLE_NODE_TYPES = frozenset({"equation", "simplification"})
_MAX_CONTRACTED_CHAIN = 3
_MAX_EXPRESSION_CHARS = 512
_MAX_EXPRESSION_OPS = 100
_MAX_SYMBOLS = 64
_MAX_SIDE_TOKENS = 12
_SYMBOL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?:\r?\n+|(?<=[.!?;])\s+)")
_EQUATION_LEAD_IN_RE = re.compile(
    r"(?:^|[,;:]|\b(?:get|gets|got|give|gives|gave|yield|yields|yielded|"
    r"so|therefore|thus|hence|become|becomes|became)\b)\s*",
    re.IGNORECASE,
)

DecisionChannel = Literal["symbolic_equivalent", "symbolic_veto", "unproved"]


@dataclass(frozen=True)
class ContractionVerdict:
    """One eligible missing reference node's fail-closed contraction result."""

    canonical_key: str
    kind: FindingKind
    predecessor_key: str
    successor_key: str
    student_node_ids: tuple[str, ...]
    evidence_spans: tuple[str, ...]
    bridge_provenance: tuple[str, ...]
    entailment: float | None
    decision_channel: DecisionChannel


def contraction_verdicts(
    student: CanonicalGraph,
    reference: ReferenceGraph,
    path: ReferencePathView,
) -> dict[str, ContractionVerdict]:
    """Return verdicts for eligible missing chains on one declared path.

    Eligibility is deliberately narrow: a run of one to three combinable,
    non-branching missing nodes must be bounded by exactly-covered student keys,
    and the student graph must bridge those bounds directly (or the same raw
    student node must cover both bounds). Each concrete equation uses a
    symbolic confirm/veto over bridge equations. Other node shapes fail closed
    to ``not_demonstrated``.
    """
    student_by_key = {node.canonical_key: node for node in student.nodes}
    reference_by_key = {node.canonical_key: node for node in reference.nodes}
    predecessor_sets, successor_sets = _path_neighbors(reference)
    keys = path.canonical_keys
    verdicts: dict[str, ContractionVerdict] = {}

    index = 1
    while index < len(keys) - 1:
        if keys[index] in student_by_key:
            index += 1
            continue
        run_start = index
        while index < len(keys) - 1 and keys[index] not in student_by_key:
            index += 1
        missing_keys = keys[run_start:index]
        predecessor_key = keys[run_start - 1]
        successor_key = keys[index]
        if not _eligible_chain(
            missing_keys,
            predecessor_key,
            successor_key,
            student_by_key,
            reference_by_key,
            predecessor_sets,
            successor_sets,
            keys,
        ):
            continue

        bridge = _bridge(student, student_by_key[predecessor_key], student_by_key[successor_key])
        if bridge is None:
            continue
        student_node_ids, evidence_spans, bridge_provenance = bridge
        for missing_key in missing_keys:
            ref_node = reference_by_key[missing_key]
            entailment: float | None = None
            symbolic_decision = _symbolic_equation_decision(ref_node, evidence_spans)
            decision_channel: DecisionChannel = "unproved"
            demonstrated = symbolic_decision is True
            if symbolic_decision is not None:
                decision_channel = "symbolic_equivalent" if symbolic_decision else "symbolic_veto"
            verdicts[missing_key] = ContractionVerdict(
                canonical_key=missing_key,
                kind=(
                    FindingKind.COVERED_BY_CONTRACTION
                    if demonstrated
                    else FindingKind.NOT_DEMONSTRATED
                ),
                predecessor_key=predecessor_key,
                successor_key=successor_key,
                student_node_ids=student_node_ids,
                evidence_spans=evidence_spans,
                bridge_provenance=bridge_provenance,
                entailment=entailment,
                decision_channel=decision_channel,
            )
    return verdicts


def _eligible_chain(
    missing_keys: tuple[str, ...],
    predecessor_key: str,
    successor_key: str,
    student_by_key: dict[str, CanonicalNode],
    reference_by_key: dict[str, CanonicalNode],
    predecessor_sets: dict[str, set[str]],
    successor_sets: dict[str, set[str]],
    path_keys: tuple[str, ...],
) -> bool:
    if not (1 <= len(missing_keys) <= _MAX_CONTRACTED_CHAIN):
        return False
    if predecessor_key not in student_by_key or successor_key not in student_by_key:
        return False
    chain = (predecessor_key, *missing_keys, successor_key)
    for offset, missing_key in enumerate(missing_keys, start=1):
        node = reference_by_key.get(missing_key)
        if node is None or node.node_type not in _COMBINABLE_NODE_TYPES:
            return False
        if predecessor_sets.get(missing_key) != {chain[offset - 1]}:
            return False
        if successor_sets.get(missing_key) != {chain[offset + 1]}:
            return False
    return all(key in path_keys for key in chain)


def _path_neighbors(
    reference: ReferenceGraph,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    predecessors: dict[str, set[str]] = {}
    successors: dict[str, set[str]] = {}
    for declared_path in reference.paths:
        for left, right in zip(
            declared_path.canonical_keys,
            declared_path.canonical_keys[1:],
            strict=False,
        ):
            successors.setdefault(left, set()).add(right)
            predecessors.setdefault(right, set()).add(left)
    return predecessors, successors


def _bridge(
    student: CanonicalGraph,
    predecessor: CanonicalNode,
    successor: CanonicalNode,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    bridging_edges = tuple(
        edge
        for edge in student.edges
        if {edge.from_key, edge.to_key} == {predecessor.canonical_key, successor.canonical_key}
    )
    merged_node_ids = set(predecessor.source_node_ids) & set(successor.source_node_ids)
    if not bridging_edges and not merged_node_ids:
        return None

    nodes = (predecessor, successor)
    student_node_ids = tuple(
        sorted({node_id for node in nodes for node_id in node.source_node_ids})
    )
    evidence_spans = tuple(
        dict.fromkeys(
            text for node in nodes for text in (*node.evidence_spans, node.symbolic) if text
        )
    )
    provenance = tuple(
        sorted(f"{edge.from_key}->{edge.to_key} ({edge.provenance})" for edge in bridging_edges)
    )
    return student_node_ids, evidence_spans, provenance


def _symbolic_equation_decision(
    reference_node: CanonicalNode,
    evidence_spans: tuple[str, ...],
) -> bool | None:
    """Return symbolic confirm/veto, or ``None`` when no proof is available."""
    reference = reference_node.symbolic
    if reference_node.node_type != "equation" or not reference or "=" not in reference:
        return None

    same_lhs_wrong = False
    for fragment in _equation_fragments(evidence_spans):
        parsed = _parse_equation_pair(fragment, reference)
        if parsed is None:
            continue
        fragment_lhs, fragment_rhs, reference_lhs, reference_rhs = parsed
        fragment_zero = fragment_lhs - fragment_rhs
        reference_zero = reference_lhs - reference_rhs
        try:
            equivalent = bool(
                sympy.simplify(fragment_zero - reference_zero) == 0
                or sympy.simplify(fragment_zero + reference_zero) == 0
            )
        except Exception:  # any symbolic failure leaves this fragment unproved
            continue
        if equivalent:
            return True
        if isinstance(reference_lhs, sympy.Symbol) and fragment_lhs == reference_lhs:
            same_lhs_wrong = True
    return False if same_lhs_wrong else None


def _equation_fragments(evidence_spans: tuple[str, ...]) -> tuple[str, ...]:
    """Extract conservative, single-equals equation runs from bridge text.

    Bridge evidence embeds algebra in prose ("... to get v2 = v1/(A1*A2) then
    just used the given value"), so each side is trimmed to its longest
    sympify-parseable run: suffixes for the left side, prefixes for the right.
    A side with no parseable run drops the fragment (fail closed).
    """
    fragments: list[str] = []
    for text in evidence_spans:
        for sentence in _SENTENCE_BOUNDARY_RE.split(text):
            if sentence.count("=") != 1:
                continue
            raw_lhs, raw_rhs = sentence.split("=", 1)
            # Common explanatory lead-ins delimit the algebraic token run in
            # prose such as "divide by A1 to get v2 = ...".
            raw_lhs = _EQUATION_LEAD_IN_RE.split(raw_lhs)[-1].strip(" `\"'")
            lhs = _longest_parseable_run(raw_lhs, keep="suffix")
            rhs = _longest_parseable_run(raw_rhs.strip(" `\"'"), keep="prefix")
            if lhs and rhs:
                fragments.append(f"{lhs} = {rhs}")
    return tuple(dict.fromkeys(fragments))


def _longest_parseable_run(side: str, *, keep: str) -> str | None:
    """The longest whitespace-token suffix/prefix of ``side`` that sympifies."""
    tokens = side.split()
    if not tokens or len(tokens) > _MAX_SIDE_TOKENS:
        tokens = tokens[-_MAX_SIDE_TOKENS:] if keep == "suffix" else tokens[:_MAX_SIDE_TOKENS]
    for count in range(len(tokens), 0, -1):
        candidate = " ".join(tokens[-count:] if keep == "suffix" else tokens[:count])
        if len(candidate) > _MAX_EXPRESSION_CHARS:
            continue
        names = set(_SYMBOL_RE.findall(candidate))
        if len(names) > _MAX_SYMBOLS:
            continue
        try:
            expression = sympy.sympify(
                candidate, locals={name: sympy.Symbol(name) for name in names}
            )
            if not isinstance(expression, sympy.Expr):
                continue  # booleans/relationals from prose keywords are not algebra
            if sympy.count_ops(expression) > _MAX_EXPRESSION_OPS:
                continue
        except Exception:  # prose or malformed algebra: try a shorter run
            continue
        return candidate
    return None


def _parse_equation_pair(
    fragment: str,
    reference: str,
) -> tuple[sympy.Expr, sympy.Expr, sympy.Expr, sympy.Expr] | None:
    """Parse one bridge/reference pair under collision-safe shared symbols."""
    if (
        fragment.count("=") != 1
        or reference.count("=") != 1
        or len(fragment) > _MAX_EXPRESSION_CHARS
        or len(reference) > _MAX_EXPRESSION_CHARS
    ):
        return None
    names = set(_SYMBOL_RE.findall(f"{fragment} {reference}"))
    if len(names) > _MAX_SYMBOLS:
        return None
    local_dict = {name: sympy.Symbol(name) for name in names}
    sides = (*fragment.split("=", 1), *reference.split("=", 1))
    try:
        parsed = tuple(sympy.sympify(side.strip(), locals=local_dict) for side in sides)
        if any(not isinstance(expression, sympy.Expr) for expression in parsed):
            return None  # booleans/relationals from prose keywords are not algebra
        if any(sympy.count_ops(expression) > _MAX_EXPRESSION_OPS for expression in parsed):
            return None
    except Exception:  # malformed or oversized symbolic input is not evidence
        return None
    # A fragment side that is a bare symbol ABSENT from the reference equation
    # is almost certainly a trimmed prose word ("v2 = the ..."), not algebra —
    # drop the fragment instead of producing a spurious veto.
    reference_names = set(_SYMBOL_RE.findall(reference))
    for expression in parsed[:2]:
        if isinstance(expression, sympy.Symbol) and str(expression) not in reference_names:
            return None
    return parsed
