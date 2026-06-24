"""Subject profiles — the spine of subject-FLUID provisioning (subject-fluid Apollo).

A ``SubjectProfile`` is a per-subject STRATEGY that declares everything
subject-specific about how a teachable reference graph is built and validated:

  * ``node_vocab``      — the allowed ``entry_type`` subset for this subject.
  * ``active_gates``    — which promotion-lint gates (1..8) RUN for this subject.
  * ``target_contract`` — ``symbol`` | ``prose`` | ``none`` (what the problem's
                          ``target_unknown`` is allowed to be).
  * ``validator``       — which reference-solution validator applies
                          (``symbolic`` = SymPy; ``faithfulness`` = grounded on
                          the authored solution).

The profile is **auto-detected ONCE** over the ingested problem set (the
profile-neutral ``detect_profile`` probe), **persisted** on the ``apollo_subjects``
row, then **read deterministically** at promotion time — so NO LLM lives in the
per-promotion control path. The gate logic itself stays PURE / DB-free / LLM-free
(``promotion_lint``); the only thing a profile contributes there is the
``active_gates`` SET that the caller (``promote``) passes in.

Two built-in profiles for v1:

  ``quantitative_symbolic`` (default, back-compat) — all 6 node types, gates 1-8,
      a SymPy ``symbol`` target. Reproduces today's fluid-mechanics behavior
      EXACTLY (the 41 seeded ss=2 ``:Canon`` still promote).
  ``qualitative_argumentative`` — the general subset
      ``procedure_step``/``definition``/``condition``, gates 1/2/3/8 + faithfulness
      (gates 4,5 OFF; 6,7 vacuous on an equation-less graph), a ``prose`` target.

FAIL-OPEN: an unknown / missing ``profile_kind``, a low-confidence probe, or any
probe error resolves to ``quantitative_symbolic`` — the STRICTEST,
back-compat-preserving profile (a false-RED quarantines a good problem; a
false-GREEN must never ship). Every fail-open is logged for review.

This module MAY touch the DB (the persistence read/write helpers import the
``Subject`` ORM + an ``AsyncSession``); the registry + dataclass + probe are PURE
and import-light so the fast unit suite needs no DB.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)

__all__ = [
    "SubjectProfile",
    "ProfileDetection",
    "PROFILE_QUANTITATIVE_SYMBOLIC",
    "PROFILE_QUALITATIVE_ARGUMENTATIVE",
    "DEFAULT_PROFILE_KIND",
    "ALL_GATES",
    "get_profile",
    "detect_profile",
    "persist_profile",
    "resolve_profile",
]

# Gate vocabulary. ``promotion_lint`` declares its own all-gates default
# independently (it must NOT import this module — it stays ORM-free); we re-declare
# the universe here so each profile spells its active set out in full.
ALL_GATES: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6, 7, 8})

PROFILE_QUANTITATIVE_SYMBOLIC = "quantitative_symbolic"
PROFILE_QUALITATIVE_ARGUMENTATIVE = "qualitative_argumentative"
# The strictest, back-compat-preserving profile every fail-open resolves to.
DEFAULT_PROFILE_KIND = PROFILE_QUANTITATIVE_SYMBOLIC

# Above this prose-fraction the probe is CONFIDENT a set is argumentative; at or
# below it we fail-open to quantitative_symbolic (ambiguity errs to the strict side).
_QUALITATIVE_PROSE_THRESHOLD = 0.6

# All six reference-solution entry types (the quantitative_symbolic node vocab).
_ALL_NODE_TYPES: frozenset[str] = frozenset(
    {
        "equation",
        "condition",
        "simplification",
        "variable_mapping",
        "procedure_step",
        "definition",
    }
)
# The general subset an argument graph is built from (no equation/simplification/
# variable_mapping). All three are already in the gate-1 mint map, so an argument
# graph passes gate 1 with NO mint-map change.
_ARGUMENT_NODE_TYPES: frozenset[str] = frozenset(
    {"procedure_step", "definition", "condition"}
)


@dataclass(frozen=True)
class SubjectProfile:
    """A per-subject strategy. Frozen / hashable so it is safe to share."""

    kind: str
    node_vocab: frozenset[str]
    active_gates: frozenset[int]
    target_contract: str  # "symbol" | "prose" | "none"
    validator: str  # "symbolic" | "faithfulness"


_QUANTITATIVE_SYMBOLIC = SubjectProfile(
    kind=PROFILE_QUANTITATIVE_SYMBOLIC,
    node_vocab=_ALL_NODE_TYPES,
    active_gates=ALL_GATES,
    target_contract="symbol",
    validator="symbolic",
)

# Gates 4 (foreign-symbol guard) and 5 (terminal-computes-symbolic-target) are the
# ONLY gates that actively break for prose arguments, so they are OFF here. Gates
# 6/7 are vacuous on an equation-less graph; we EXCLUDE them from the active set
# too (rather than rely on vacuity) so the prose path never reads given_values /
# target_unknown as if they were symbolic. Faithfulness is a SEPARATE gate
# (pairing_gate, LLM-grounded on the authored solution), not one of the pure 1-8.
_QUALITATIVE_ARGUMENTATIVE = SubjectProfile(
    kind=PROFILE_QUALITATIVE_ARGUMENTATIVE,
    node_vocab=_ARGUMENT_NODE_TYPES,
    active_gates=frozenset({1, 2, 3, 8}),
    target_contract="prose",
    validator="faithfulness",
)

_REGISTRY: dict[str, SubjectProfile] = {
    _QUANTITATIVE_SYMBOLIC.kind: _QUANTITATIVE_SYMBOLIC,
    _QUALITATIVE_ARGUMENTATIVE.kind: _QUALITATIVE_ARGUMENTATIVE,
}


def get_profile(kind: str | None) -> SubjectProfile:
    """Resolve a profile by ``kind``, FAILING OPEN to ``quantitative_symbolic``.

    A ``None`` (un-detected subject) or an unknown kind (a corpus tagged with a
    profile this build does not ship) both resolve to the strict default — the
    safe direction — and the unknown case is logged for review.
    """
    if kind is None:
        return _QUANTITATIVE_SYMBOLIC
    profile = _REGISTRY.get(kind)
    if profile is None:
        _LOG.warning(
            "subject_profile_unknown_kind",
            extra={
                "event": "subject_profile_unknown_kind",
                "kind": kind,
                "fail_open_to": DEFAULT_PROFILE_KIND,
            },
        )
        return _QUANTITATIVE_SYMBOLIC
    return profile


# --------------------------------------------------------------------------- #
# Profile-neutral detection probe (PURE — heuristic, DB-free, LLM-free)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProfileDetection:
    """Outcome of the detection probe — exactly the three columns persisted on
    ``apollo_subjects`` (``profile_kind`` / ``profile_confidence`` /
    ``profile_evidence``)."""

    kind: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


# Crude math signal: an (in)equality, an integral/sum/sqrt, a LaTeX math macro, an
# arithmetic operator BETWEEN digits, or a number carrying a physical unit. Prose
# argument statements ("Explain why federalism disperses power...") match none of
# these; a worked physics problem matches several.
_MATH_SIGNAL = re.compile(
    r"[=≤≥<>∫∑√≈]"
    r"|\\(?:frac|int|sum|sqrt|cdot|times|partial|nabla)"
    r"|\$[^$]+\$"
    r"|\d+\s*[+\-*/^]\s*\d"
    r"|\b\d+(?:\.\d+)?\s*(?:m|kg|s|N|Pa|J|W|V|A|mol|K|m/s|m\^2|m\^3)\b"
)


def _step_entry_types(problem: Mapping[str, Any]) -> set[str]:
    steps = problem.get("reference_solution") or problem.get("worked_procedure") or []
    out: set[str] = set()
    if isinstance(steps, Iterable):
        for s in steps:
            if isinstance(s, Mapping):
                et = s.get("entry_type")
                if isinstance(et, str):
                    out.add(et)
    return out


def _problem_is_symbolic(problem: Mapping[str, Any]) -> bool:
    """One problem's symbolic-vs-prose vote (True = symbolic).

    Symbolic if it carries ANY of: a typed ``equation``/``variable_mapping`` step,
    a non-empty numeric ``given_values`` map, or a textual math signal in its
    statement / solution text.
    """
    if {"equation", "variable_mapping"} & _step_entry_types(problem):
        return True
    gv = problem.get("given_values")
    if isinstance(gv, Mapping) and len(gv) > 0:
        return True
    text = " ".join(
        str(problem.get(k) or "")
        for k in ("statement", "problem_text", "solution", "answer", "worked")
    )
    return bool(_MATH_SIGNAL.search(text))


def detect_profile(problems: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]]) -> ProfileDetection:
    """Profile-neutral probe over an ingested problem set — PURE / DB-free / LLM-free.

    Counts each problem's symbolic-vs-prose vote and decides:

      * a decisively-prose set (prose fraction >= ``_QUALITATIVE_PROSE_THRESHOLD``)
        -> ``qualitative_argumentative`` with that fraction as confidence;
      * anything else (clearly symbolic OR ambiguous OR empty) -> FAIL-OPEN to
        ``quantitative_symbolic`` (the strict, back-compat default).

    NEVER raises: any error (a malformed record, a non-iterable) is caught and
    fails open, logged, so a bad probe can never abort ingest.
    """
    try:
        items = [p for p in problems if isinstance(p, Mapping)]
        total = len(items)
        if total == 0:
            _LOG.info(
                "subject_profile_probe_empty",
                extra={"event": "subject_profile_probe_empty", "fail_open_to": DEFAULT_PROFILE_KIND},
            )
            return ProfileDetection(
                kind=DEFAULT_PROFILE_KIND,
                confidence=0.0,
                evidence={"reason": "empty problem set", "n_problems": 0, "fail_open": True},
            )

        symbolic_count = sum(1 for p in items if _problem_is_symbolic(p))
        prose_count = total - symbolic_count
        prose_fraction = prose_count / total
        evidence = {
            "n_problems": total,
            "n_symbolic": symbolic_count,
            "n_prose": prose_count,
            "prose_fraction": round(prose_fraction, 4),
            "threshold": _QUALITATIVE_PROSE_THRESHOLD,
        }

        if prose_fraction >= _QUALITATIVE_PROSE_THRESHOLD:
            return ProfileDetection(
                kind=PROFILE_QUALITATIVE_ARGUMENTATIVE,
                confidence=round(prose_fraction, 4),
                evidence=evidence,
            )
        # Clearly symbolic OR ambiguous -> the strict default (fail-open on doubt).
        evidence["fail_open"] = prose_fraction > 0.0
        return ProfileDetection(
            kind=PROFILE_QUANTITATIVE_SYMBOLIC,
            confidence=round(1.0 - prose_fraction, 4),
            evidence=evidence,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open is the whole point
        _LOG.warning(
            "subject_profile_probe_error",
            extra={
                "event": "subject_profile_probe_error",
                "error_class": type(exc).__name__,
                "fail_open_to": DEFAULT_PROFILE_KIND,
            },
        )
        return ProfileDetection(
            kind=DEFAULT_PROFILE_KIND,
            confidence=0.0,
            evidence={"reason": f"probe error: {type(exc).__name__}", "fail_open": True},
        )


# --------------------------------------------------------------------------- #
# Persistence (DB read/write) — these MAY touch the session
# --------------------------------------------------------------------------- #


async def persist_profile(db, subject_id: int, detection: ProfileDetection) -> None:
    """Write a detection onto the ``apollo_subjects`` row (kind+confidence+evidence).

    Flushes (does NOT commit — the caller owns the transaction). Raises if the
    subject row is missing (a programming error: detection runs after the subject
    exists).
    """
    from apollo.persistence.models import Subject  # local import keeps probe import-light

    subject = await db.get(Subject, subject_id)
    if subject is None:
        raise RuntimeError(f"persist_profile: subject {subject_id} not found")
    subject.profile_kind = detection.kind
    subject.profile_confidence = detection.confidence
    subject.profile_evidence = dict(detection.evidence)
    await db.flush()
    _LOG.info(
        "subject_profile_persisted",
        extra={
            "event": "subject_profile_persisted",
            "subject_id": subject_id,
            "profile_kind": detection.kind,
            "profile_confidence": detection.confidence,
        },
    )


async def resolve_profile(db, subject_id: int) -> SubjectProfile:
    """Read the persisted ``profile_kind`` for a subject and resolve its profile.

    FAILS OPEN to ``quantitative_symbolic`` when the subject is missing or carries
    no (None) ``profile_kind`` — the back-compat default a freshly-migrated row
    already backfills to.
    """
    from apollo.persistence.models import Subject  # local import keeps probe import-light

    subject = await db.get(Subject, subject_id)
    kind = getattr(subject, "profile_kind", None) if subject is not None else None
    return get_profile(kind)
