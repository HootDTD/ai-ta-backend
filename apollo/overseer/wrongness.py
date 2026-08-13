"""Apollo P3.2 — the pure wrongness core: what a finding IS and what it selects.

One module owns the meaning of "the student said something materially wrong",
so no consumer re-derives the predicate. Three jobs, no IO:

1. **The single flag reader.** ``effective_wrongness_level`` is the only place
   the ladder flag is read, and it pairs the level with the concept allowlist
   exactly like INTERACTION5 does at the Done call site.
2. **Ledger → findings.** ``ledger_findings`` turns ``QuestionOpportunity``-shaped
   rows into flat, total, duck-typed value objects.
3. **S2′ + the ladder.** ``select_findings`` evaluates the consequence predicate
   and ``candidate_quotes`` builds the corroborator's candidate map.

**Import-light on purpose.** stdlib + ``config.settings`` only. Importing
``topic_score`` / ``transcript_coverage`` / ``smart_questions.unified`` here
would close the cycle ``unified → selection → topic_score → wrongness →
settings``, so the one shared constant (``CEILING_UNCORRECTED``) is duplicated
and pinned equal by test instead of imported.

**Every function is total.** ``question_opportunities.evidence`` is a free-form
JSONB array with no CHECK beyond ``jsonb_typeof = 'array'``; anything malformed
coerces or is skipped and never raises, because this feeds the grade path and a
``Done`` must not 500 on a bad row.

Spec: ``.planning/specs/2026-08-12-apollo-p32-wrongness-signal-design.md``
§2.2 (S2′), §2.3 (the ladder), §2.6 (the flag), §8 D4/D7.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from config.settings import interaction_allowed_for_concept, wrongness_level

_LOG = logging.getLogger(__name__)

# The three producer labels (mirrors ``smart_questions.unified.Wrongness``,
# duplicated rather than imported to keep this module import-light).
WRONGNESS_NONE = "none"
WRONGNESS_SELF = "contradicts_self"
WRONGNESS_MATERIAL = "contradicts_material"
WRONGNESS_VALUES: frozenset[str] = frozenset({WRONGNESS_NONE, WRONGNESS_SELF, WRONGNESS_MATERIAL})

# S2′: only a node the grader already CREDITED can be over-credited, and 0.6 is
# the lowest P1.1 adjudication anchor meaning "the student landed this". Below
# it there is no unearned credit to contest — which is also what keeps
# INTERACTION5's flat 0.5 aside cap from ever stacking with a wrongness finding.
MIN_CORROBORATED_CREDIT = 0.6

# Level-4 (dark) ceiling. ``topic_score.CEILING_UNCORRECTED`` is the authority;
# this copy exists only so the pure core stays import-light, and
# ``test_wrongness_predicate.test_ceiling_constant_agrees_with_topic_score``
# fails the build if the two ever diverge.
CEILING_UNCORRECTED = 84

# The ladder rungs (spec §2.3 / plan S10). Named so no consumer writes a bare
# ``>= 2``: every gate site compares ``effective_wrongness_level`` to one of these.
LEVEL_OFF = 0
LEVEL_PRODUCE = 1  # produce + persist + shadow-log (no serving change)
LEVEL_SCHEDULE = 2  # probe priority + done-gate + carried challenge
LEVEL_SURFACE = 3  # narrative line + teacher surfaces + XP bonus
LEVEL_CEILING = 4  # the bounded score ceiling (built dark; nothing sets it)
MAX_LEVEL = LEVEL_CEILING


def effective_wrongness_level(concept_slug: str | None) -> int:
    """THE single reader of the ladder flag: clamped level, 0 outside the pilot.

    Returns ``wrongness_level()`` clamped to 0-``MAX_LEVEL``, forced to 0 unless
    ``interaction_allowed_for_concept(concept_slug)`` admits this problem's
    concept — the INTERACTION5 pairing, so the ladder can pilot on one concept
    without touching anyone else's grade. Every gate site in the build calls
    this; nothing else reads the env var.
    """
    return (
        max(LEVEL_OFF, min(MAX_LEVEL, wrongness_level()))
        if interaction_allowed_for_concept(concept_slug)
        else LEVEL_OFF
    )


@dataclass(frozen=True)
class LedgerFinding:
    """One evidence entry on one tally row, flattened.

    ``turn_id`` is the student turn the quote came from; ``is_latest_evidence``
    marks the node's MOST RECENT usable entry (S2′ keys on recency, never on the
    sticky ``conflicting`` state). ``last_asked_turn`` is additive to the frozen
    S6 shape and defaulted — it is the only input the decision-7 Apollo-elicited
    guard needs and it lives on the same row.
    """

    node_id: str
    wrongness: str
    quote: str
    contradicts: str
    kind: str
    turn_id: int
    is_latest_evidence: bool
    state: str
    times_asked: int
    last_asked_turn: int | None = None


@dataclass(frozen=True)
class WrongnessFinding:
    """A ledger finding after S2′ and the ladder have been evaluated.

    ``corroborated`` is the ONLY score-relevant bit (and it is inert below level
    4). ``resolved`` / ``apollo_elicited`` drive the decision-7 XP bonus, which
    celebrates a student who FIXED a claim Apollo challenged — note that a
    resolved finding is never corroborated, by construction: S2′ requires
    ``NOT corrected_later``. ``would_ceiling`` is the shadow-log counterfactual
    ("this would have been ceilinged at level 4") and moves nothing.
    """

    node_id: str
    quote: str
    contradicts: str
    kind: str
    corroborated: bool
    resolved: bool
    apollo_elicited: bool
    would_ceiling: bool


def _coerce_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_credit(value: Any) -> float:
    """Fail-safe = miss: an absent or unparseable credit reads as 0.0, which is
    below ``MIN_CORROBORATED_CREDIT`` and therefore never corroborates."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_true(value: Any) -> bool:
    """Genuine ``True`` only — mirrors ``transcript_coverage._verdict_bool``'s
    strictness so a truthy string from a hand-edited payload cannot corroborate."""
    return value is True


def _coerce_wrongness(value: Any, *, state: str) -> str:
    """Off-enum labels coerce to ``none``. A ``missing`` node cannot carry a
    contradiction: the producer's verbatim gate drops every non-``missing``
    update without a span, so a wrongness on ``missing`` is impossible by
    construction and is treated as absent rather than trusted."""
    if state == "missing":
        return WRONGNESS_NONE
    return value if value in WRONGNESS_VALUES else WRONGNESS_NONE


def _row_findings(row: Any) -> list[LedgerFinding]:
    raw_node_id = getattr(row, "reference_node_id", None)
    if raw_node_id is None:
        return []
    node_id = str(raw_node_id)
    if not node_id:
        return []
    state = _coerce_str(getattr(row, "state", None))
    times_asked = _coerce_int(getattr(row, "times_asked", 0))
    last_asked_turn = _coerce_optional_int(getattr(row, "last_asked_turn", None))

    evidence = getattr(row, "evidence", None)
    if not isinstance(evidence, list):
        return []

    usable: list[dict[str, Any]] = [
        entry
        for entry in evidence
        if isinstance(entry, dict) and _coerce_str(entry.get("quote")).strip()
    ]
    return [
        LedgerFinding(
            node_id=node_id,
            wrongness=_coerce_wrongness(entry.get("wrongness"), state=state),
            quote=_coerce_str(entry.get("quote")),
            contradicts=_coerce_str(entry.get("contradicts")),
            kind=_coerce_str(entry.get("kind")),
            turn_id=_coerce_int(entry.get("turn_id")),
            is_latest_evidence=index == len(usable) - 1,
            state=state,
            times_asked=times_asked,
            last_asked_turn=last_asked_turn,
        )
        for index, entry in enumerate(usable)
    ]


def ledger_findings(rows: Iterable[Any]) -> tuple[LedgerFinding, ...]:
    """Flatten ``QuestionOpportunity``-shaped rows into per-evidence findings.

    Duck-typed on ``.reference_node_id``, ``.state``, ``.times_asked``,
    ``.last_asked_turn`` and ``.evidence`` so the harness, the tests, and the ORM
    all work unchanged. Entries are emitted in ledger order (the controller
    appends in turn order), untagged two-key entries included with
    ``wrongness == "none"`` — callers filter. Only the LAST entry carrying a
    usable quote is ``is_latest_evidence``, matching
    ``done._latest_student_quote``'s "most recent demonstration" rule.

    Pure and total: a malformed row or entry is skipped, never raised.
    """
    findings: list[LedgerFinding] = []
    for row in rows:
        try:
            findings.extend(_row_findings(row))
        except Exception as exc:  # never break a Done on one unreadable row
            _LOG.warning("apollo_wrongness_ledger_row_skipped err=%s", exc)
    return tuple(findings)


def candidate_quotes(
    findings: Iterable[LedgerFinding],
    *,
    graded_node_ids: Collection[str],
) -> dict[str, str]:
    """The corroborator's ``wrongness_candidates`` map: ``{node_id: quote}``.

    Only ``contradicts_material`` on a GRADED node, and only from the node's most
    recent evidence entry — the adjudicator is a corroborator that may confirm or
    deny a claim the tally already raised, and may never originate one. An empty
    map leaves the adjudication schema, prompts, and coverage dict byte-identical
    to today.
    """
    return {
        finding.node_id: finding.quote
        for finding in findings
        if finding.wrongness == WRONGNESS_MATERIAL
        and finding.is_latest_evidence
        and finding.node_id in graded_node_ids
        and finding.quote
    }


def select_findings(
    *,
    findings: Iterable[LedgerFinding],
    credits: Mapping[str, float],
    second_reader: Mapping[str, Mapping[str, bool]],
    graded_node_ids: Collection[str],
    raw_score: int,
) -> tuple[WrongnessFinding, ...]:
    """Evaluate S2′ and the ladder over every non-``none`` ledger finding.

    **S2′ — a finding is ``corroborated`` iff ALL of:**

    * ``wrongness == "contradicts_material"`` (self-contradiction is a dialogue
      artifact, not a material error), AND
    * ``credits[node_id] >= MIN_CORROBORATED_CREDIT`` — only credited work can be
      over-credited, AND
    * it is the node's MOST RECENT evidence entry — ``conflicting`` is a sticky
      final state, so keying on state would punish a student who later revised,
      AND
    * the second reader returned ``contradicted AND NOT corrected_later``, AND
    * the node is graded (``_GRADED_NODE_TYPES``, resolved by the caller).

    A naive "final state ``conflicting`` ⇒ dock" rule scored 0/2 on the only two
    high-credit cases it fired on in prod (attempt 86, a zero-transcript
    artifact; attempt 167, a student self-correcting after two probes). S2′
    selects neither.

    **Fail-safe = miss.** An absent second-reader row, an absent credit, or an
    abstained node never corroborates — the corroborator's silence can only ever
    remove a consequence, never create one.

    Ungraded-type nodes are still REPORTED (they feed the narrative and the
    teacher surfaces) and can never be corroborated: moving them silently widens
    the graded denominator, which is P1.4's decision, not this one.

    **``apollo_elicited`` is a presence test, deliberately.** Spec §8 D7 writes
    the guard as ``last_asked_turn < correction_turn``, but **the correction turn
    is not on the ledger**: the shipped correction signal is the second reader's
    ``corrected_later`` BOOLEAN, which carries no turn, and a correction that
    leaves the sticky ``conflicting`` state in place appends no new tally entry
    to compare against either. Two candidate substitutes, and why this one:

    * ``last_asked_turn < turn_id`` ("asked before the wrong CLAIM") reads
      stricter, but ``last_asked_turn`` is a mutable row-level LAST value, so
      the ordinary challenge loop — student errs, Apollo probes the contested
      node, student fixes it — pushes it PAST the claim turn and flips the guard
      to ``False``. It is defeated by exactly the L2a probe priority that exists
      to produce corrections, which would leave decision 7 an unpayable no-op in
      its target population.
    * ``last_asked_turn is not None`` ("Apollo asked about this node at all
      during this attempt") is MONOTONE — no later probe can turn it off — and
      still blocks the named farm, which is the node Apollo never asked about.
      It also equals the spec's comparison wherever that comparison is
      reachable: a node the tally relabels ``understood`` on correction leaves
      ``probeable_graded`` (`selection.build_selection_policy`), so Apollo
      cannot ask about it after the correction.

    Magnitude is bounded independently of which reading wins: the bonus is +10,
    deduped once per user x problem x node against ``prior_wrongness_findings``,
    and XP only ever goes up.
    """
    selected: list[WrongnessFinding] = []
    # The whole map, not only its values: `second_reader` is
    # `coverage["wrongness"]`, i.e. LLM-shaped data that `validate_coverage_verdict`
    # admits as an OPTIONAL key without inspecting its type. A model returning
    # `"wrongness": []` or a bare string would make `.get` an AttributeError —
    # raised on the grade path AFTER the Done claim is taken. This module's
    # contract is that every function is total; that has to include its own
    # argument, not just the rows inside it.
    readers: Mapping[str, Any] = second_reader if isinstance(second_reader, Mapping) else {}
    for finding in findings:
        if finding.wrongness == WRONGNESS_NONE:
            continue
        reader = readers.get(finding.node_id)
        row: Mapping[str, Any] = reader if isinstance(reader, Mapping) else {}
        contradicted = _is_true(row.get("contradicted"))
        corrected_later = _is_true(row.get("corrected_later"))
        corroborated = (
            finding.wrongness == WRONGNESS_MATERIAL
            and _coerce_credit(credits.get(finding.node_id)) >= MIN_CORROBORATED_CREDIT
            and finding.is_latest_evidence
            and contradicted
            and not corrected_later
            and finding.node_id in graded_node_ids
        )
        selected.append(
            WrongnessFinding(
                node_id=finding.node_id,
                quote=finding.quote,
                contradicts=finding.contradicts,
                kind=finding.kind,
                corroborated=corroborated,
                resolved=corrected_later,
                # Decision 7: the bonus only ever rewards a contradiction Apollo
                # ELICITED. Asserting something wrong on a node Apollo never
                # asked about and then fixing it yourself is a farmable path and
                # earns nothing. See the note below on why this is a presence
                # test and not a turn comparison.
                apollo_elicited=finding.last_asked_turn is not None,
                would_ceiling=(
                    corroborated and not corrected_later and raw_score > CEILING_UNCORRECTED
                ),
            )
        )
    return tuple(selected)


__all__ = [
    "CEILING_UNCORRECTED",
    "LEVEL_CEILING",
    "LEVEL_OFF",
    "LEVEL_PRODUCE",
    "LEVEL_SCHEDULE",
    "LEVEL_SURFACE",
    "MAX_LEVEL",
    "MIN_CORROBORATED_CREDIT",
    "WRONGNESS_MATERIAL",
    "WRONGNESS_NONE",
    "WRONGNESS_SELF",
    "WRONGNESS_VALUES",
    "LedgerFinding",
    "WrongnessFinding",
    "candidate_quotes",
    "effective_wrongness_level",
    "ledger_findings",
    "select_findings",
]
