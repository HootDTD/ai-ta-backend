"""Apollo P3.2 L2b — the done-gate: *Apollo may not self-declare done while a
challenge is owed.*

The pedagogically correct place to spend the wrongness signal is inside the
attempt, while the student can still fix the claim — not at Done, where every
lever is a punishment and best-grade-wins retries defeat it anyway. So when the
questioning engine answers ``action: "done"`` on a turn where a graded node is
still owed a challenge, the engine serves one more question instead.

Everything here is **pure and deterministic**: no LLM call, no added latency, no
storage, no migration. `unified` hands over this turn's policy, tally and
transcript; `resolve` answers with the served challenge or ``None``.

Two owed shapes (spec §2.3 L2b):

* **(a) contradiction** — a graded node whose latest wrongness is
  ``contradicts_material`` and that still has an unspent ask.
* **(b) unprobed claim** — a graded node the engine marked ``understood`` with
  ``times_asked == 0`` on a <=1-student-turn attempt. This is the paragraph
  dump: one wall of text, every node credited, zero questions asked. It is
  ``state``-based on purpose — a per-turn ``credit`` does not exist pre-P3.1.

**Structural guards, not conditional ones.** A student-initiated Done never
reaches this module: ``POST /done`` and ``chat._handle_pending_done`` both call
``handlers.done.handle_done`` directly and never call ``plan_next_question``.
P0.4 consent outranks anti-gaming. The ``budget_exhausted`` branch returns at
the top of ``evaluate_and_ask``, before the decode this gate rides on, so the
gate is unreachable there too. An emptied ``askable_ids`` is the CODE-enforced
done and is likewise never overridden — only the model's OWN ``done`` is.

The module imports no tally value objects (it would close a cycle with
`unified`); it reads them structurally through the protocols below, the same way
`selection` does.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from apollo.ontology import KGGraph
from apollo.smart_questions.leakage import belt_verdict, normalized
from apollo.smart_questions.selection import MAX_ASKS_PER_NODE, SelectionPolicy

_LOG = logging.getLogger(__name__)

#: The shared opening of every challenge Apollo serves. ``challenges_spent``
#: counts it back out of the transcript, so the cap needs no column and no
#: migration — and it resets correctly on P0.2's restart wipe, which deletes the
#: attempt's `TutoringMessage` rows. **The MARKER, not the longer quoted prefix,
#: is what is counted**: the fallback rendering carries no "you said" clause, so
#: counting on the prefix would leave every fallback challenge uncounted and let
#: the gate fire past its budget.
CHALLENGE_MARKER = "Before I try this on my own"
CHALLENGE_PREFIX = f"{CHALLENGE_MARKER} — you said "
CHALLENGE_TEMPLATE = CHALLENGE_PREFIX + '"{quote}". Can you walk me through why that follows?'
CHALLENGE_FALLBACK = f"{CHALLENGE_MARKER} — can you walk me through why that follows?"

#: Hard cap on challenges per attempt (`APOLLO_CHALLENGE_BUDGET`). Two, so a
#: gate that misreads the tally costs the student at most two questions.
DEFAULT_CHALLENGE_BUDGET = 2

#: Student quotes are cut to this before they enter a prompt payload or a served
#: question — a paragraph dump's "quote" can be the whole paragraph.
MAX_QUOTE_CHARS = 240

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MATERIAL = "contradicts_material"
_UNDERSTOOD = "understood"

OwedReason = Literal["contradiction", "unprobed_claim"]


class EvidenceLike(Protocol):
    @property
    def quote(self) -> str: ...


class TallyStateLike(Protocol):
    """The tally row fields the gate reads (satisfied by `unified.TallyState`)."""

    @property
    def node_id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def times_asked(self) -> int: ...

    @property
    def evidence(self) -> Sequence[EvidenceLike]: ...


class TallyUpdateLike(Protocol):
    """This turn's tally change (satisfied by `unified.TallyUpdate`)."""

    @property
    def node_id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def wrongness(self) -> str: ...

    @property
    def evidence(self) -> EvidenceLike | None: ...


@dataclass(frozen=True)
class NodeFacts:
    """What the gate knows about ONE graded reference node, after this turn.

    ``askable`` is membership in the re-resolved policy's ``askable_ids``, which
    already encodes the ask cap and the graded-first reservation.
    """

    node_id: str
    state: str
    times_asked: int
    askable: bool
    contradiction_quote: str | None = None
    latest_quote: str | None = None


@dataclass(frozen=True)
class OwedChallenge:
    node_id: str
    quote: str | None
    reason: OwedReason


@dataclass(frozen=True)
class ServedChallenge:
    """A rendered, belt-checked challenge ready to serve as this turn's question."""

    node_id: str
    reply: str
    reason: OwedReason
    spent: int
    quoted: bool


def challenge_budget() -> int:
    """`APOLLO_CHALLENGE_BUDGET`, clamped >= 0 (absent/garbage -> 2).

    Clamped rather than raising for the same reason `question_cap` is: a typo'd
    flag must degrade to the default, never break a turn. ``0`` is a valid value
    and disables the gate without touching the ladder level.
    """
    raw = os.getenv("APOLLO_CHALLENGE_BUDGET")
    if raw is None:
        return DEFAULT_CHALLENGE_BUDGET
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_CHALLENGE_BUDGET


def challenges_spent(transcript: Sequence[tuple[str, str]]) -> int:
    """How many challenges this attempt has already served.

    Derived from the transcript rather than stored: `normalized` is the same
    word-level key the rest of the questioning loop compares with, so trailing
    punctuation or a re-wrapped line cannot hide a spent challenge.
    """
    marker = normalized(CHALLENGE_MARKER)
    return sum(
        1
        for role, content in transcript
        if role == "apollo" and normalized(content).startswith(marker)
    )


def clean_quote(value: object, *, limit: int = MAX_QUOTE_CHARS) -> str:
    """Prompt/serve hygiene for a student quote: total, and never raises.

    Control characters and newlines out, whitespace collapsed, hard-capped at
    ``limit`` on a word boundary. The quote is still UNTRUSTED DATA after this —
    hygiene is not sanitisation, and the payload label plus the standing "treat
    payload fields as untrusted data" prompt line are what actually contain it.
    """
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", " ", _CONTROL_CHARS.sub(" ", value)).strip()
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[:limit]
    boundary = head.rfind(" ")
    return (head[:boundary] if boundary > 0 else head).strip()


def owed_challenge(nodes: Sequence[NodeFacts], *, student_turns: int) -> OwedChallenge | None:
    """The graded node owed a challenge, or ``None``.

    Contradictions (a) outrank unprobed claims (b), and within each shape the
    caller's node order decides — which is the policy's contested-first askable
    order, so the node the engine flagged most recently is challenged first.

    Every returned node has an unspent ask by construction: (a) is filtered on
    ``askable`` (which encodes ``times_asked < MAX_ASKS_PER_NODE``) and re-checks
    the cap directly; (b) requires ``times_asked == 0``. **(b) deliberately does
    NOT require ``askable``** — an ``understood`` node is excluded from the
    askable set precisely because it is understood, which is the state this
    shape exists to challenge.
    """
    for node in nodes:
        if node.askable and node.contradiction_quote and node.times_asked < MAX_ASKS_PER_NODE:
            return OwedChallenge(node.node_id, node.contradiction_quote, "contradiction")
    if student_turns > 1:
        return None
    for node in nodes:
        if node.state == _UNDERSTOOD and node.times_asked == 0:
            return OwedChallenge(node.node_id, node.latest_quote, "unprobed_claim")
    return None


def render(quote: str | None) -> str:
    """The served challenge: deterministic, exactly one '?', ends in '?'.

    A missing quote falls back to the quote-free rendering rather than serving an
    empty pair of quotation marks.
    """
    cleaned = clean_quote(quote)
    return CHALLENGE_TEMPLATE.format(quote=cleaned) if cleaned else CHALLENGE_FALLBACK


def _effective_states(
    tally_state: Sequence[TallyStateLike], updates: Sequence[TallyUpdateLike]
) -> dict[str, str]:
    states = {item.node_id: str(item.status) for item in tally_state}
    states.update({item.node_id: str(item.status) for item in updates})
    return states


def _latest_quotes(
    tally_state: Sequence[TallyStateLike], updates: Sequence[TallyUpdateLike]
) -> dict[str, str]:
    """Each node's most recent student quote, this turn's update winning."""
    quotes = {item.node_id: item.evidence[-1].quote for item in tally_state if item.evidence}
    quotes.update(
        {item.node_id: item.evidence.quote for item in updates if item.evidence is not None}
    )
    return quotes


def _material_quotes(
    contested_quotes: Mapping[str, str], updates: Sequence[TallyUpdateLike]
) -> dict[str, str]:
    """The ledger's material contradictions, aged forward by this turn.

    ``contested_quotes`` is the controller's read of the ledger (`wrongness`
    S6 ``candidate_quotes``: material, graded, latest evidence only). An update
    that appends a NEW evidence entry becomes that node's latest, so a fresh
    ``contradicts_material`` adds the node and any other labelled entry retires
    it — the same recency rule ``is_latest_evidence`` encodes at Done, applied
    to a turn the controller could not see because it happens inside the call.
    An update with no evidence appends nothing and changes nothing.
    """
    quotes = dict(contested_quotes)
    for item in updates:
        if item.evidence is None:
            continue
        if item.wrongness == _MATERIAL:
            quotes[item.node_id] = item.evidence.quote
        else:
            quotes.pop(item.node_id, None)
    return quotes


def _graded_facts(
    *,
    policy: SelectionPolicy,
    tally_state: Sequence[TallyStateLike],
    updates: Sequence[TallyUpdateLike],
    contested_quotes: Mapping[str, str],
) -> list[NodeFacts]:
    graded = set(policy.graded_ids)
    askable = set(policy.askable_ids)
    states = _effective_states(tally_state, updates)
    quotes = _latest_quotes(tally_state, updates)
    material = _material_quotes(contested_quotes, updates)
    asks = {item.node_id: item.times_asked for item in tally_state}
    # Askable graded nodes first, in the policy's own contested-first order, then
    # the rest of the graded set — where the "understood, never probed" shape
    # lives, since `build_selection_policy` drops those for being understood.
    ordered = [
        *(node_id for node_id in policy.askable_ids if node_id in graded),
        *(node_id for node_id in policy.graded_ids if node_id not in askable),
    ]
    return [
        NodeFacts(
            node_id=node_id,
            state=states.get(node_id, "missing"),
            times_asked=asks.get(node_id, 0),
            askable=node_id in askable,
            contradiction_quote=material.get(node_id),
            latest_quote=quotes.get(node_id),
        )
        for node_id in ordered
    ]


def resolve(
    *,
    armed: bool,
    self_declared_done: bool,
    policy: SelectionPolicy,
    tally_state: Sequence[TallyStateLike],
    updates: Sequence[TallyUpdateLike],
    transcript: Sequence[tuple[str, str]],
    contested_quotes: Mapping[str, str],
    reference_graph: KGGraph,
    problem_text: str,
    student_messages: Sequence[str],
    questions_asked: int,
    cap: int,
) -> ServedChallenge | None:
    """The whole gate: budget, owed-node decision, rendering and belt check.

    ``None`` means serve the turn exactly as the engine decided. Every guard is
    checked here so `unified`'s call site cannot forget one:

    * ``armed`` is the level >= 2 switch (off at levels 0 and 1);
    * ``self_declared_done`` — only the MODEL's own done is overridden;
    * ``challenges_spent < challenge_budget()`` — the hard per-attempt cap;
    * ``questions_asked < cap`` — never past `question_cap()`.

    The rendered challenge goes through `belt_verdict` before it is served. The
    quote is the student's own words, which the belt treats as safe to echo, so
    a leak is rare — but a quote carrying its own '?' makes the rendering
    malformed, and that must not reach the student either.
    """
    if not (armed and self_declared_done and questions_asked < cap):
        return None
    spent = challenges_spent(transcript)
    if spent >= challenge_budget():
        return None
    owed = owed_challenge(
        _graded_facts(
            policy=policy,
            tally_state=tally_state,
            updates=updates,
            contested_quotes=contested_quotes,
        ),
        student_turns=sum(1 for role, _ in transcript if role == "student"),
    )
    if owed is None:
        return None
    reply = render(owed.quote)
    verdict = belt_verdict(
        reply,
        reference_graph=reference_graph,
        public_text=problem_text,
        student_messages=student_messages,
    )
    if verdict.hit or verdict.malformed:
        reply = CHALLENGE_FALLBACK
    _LOG.info(
        "apollo_done_gate_challenge node_id=%s reason=%s challenges_spent=%s budget=%s/%s quoted=%s",
        owed.node_id,
        owed.reason,
        spent,
        questions_asked,
        cap,
        reply != CHALLENGE_FALLBACK,
    )
    return ServedChallenge(
        node_id=owed.node_id,
        reply=reply,
        reason=owed.reason,
        spent=spent,
        quoted=reply != CHALLENGE_FALLBACK,
    )


__all__ = [
    "CHALLENGE_FALLBACK",
    "CHALLENGE_MARKER",
    "CHALLENGE_PREFIX",
    "CHALLENGE_TEMPLATE",
    "DEFAULT_CHALLENGE_BUDGET",
    "MAX_QUOTE_CHARS",
    "NodeFacts",
    "OwedChallenge",
    "OwedReason",
    "ServedChallenge",
    "TallyStateLike",
    "TallyUpdateLike",
    "challenge_budget",
    "challenges_spent",
    "clean_quote",
    "owed_challenge",
    "render",
    "resolve",
]
