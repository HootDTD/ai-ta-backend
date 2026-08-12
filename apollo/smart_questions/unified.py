"""One-call Apollo tally updates and question generation with log-only belt telemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from apollo.agent._llm import bounded_client
from apollo.ontology import KGGraph
from apollo.smart_questions import challenge, prompts
from apollo.smart_questions.leakage import WORD_RE, BeltVerdict, belt_verdict, normalized
from apollo.smart_questions.selection import (
    SelectionPolicy,
    build_selection_policy,
    is_graded,
    ordered_nodes,
)

LearnerState = Literal["understood", "tentative", "missing", "conflicting"]
#: P3.2 — the per-update wrongness label. The tally is the SOLE producer: its
#: quotes are verbatim-gated in code (`_verbatim_span`), while the at-Done
#: adjudicator populates no spans at all, so anything student-facing or
#: score-moving must be anchored here. The at-Done adjudicator only ever
#: corroborates a label this engine raised; it never originates one.
Wrongness = Literal["none", "contradicts_self", "contradicts_material"]
FallbackReason = Literal[
    "malformed_regenerated",
    "malformed_exhausted",
    "budget_exhausted",
    "off_policy_regenerated",
    "off_policy_exhausted",
    "no_probeable_node",
]

_VALID_STATES: set[str] = {"understood", "tentative", "missing", "conflicting"}
WRONGNESS_VALUES: frozenset[str] = frozenset({"none", "contradicts_self", "contradicts_material"})
_DEFAULT_MODEL = "gpt-5.2"
_DEFAULT_REASONING_EFFORT = "medium"
_DEFAULT_QUESTION_CAP = 8
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceQuote:
    turn_id: int
    quote: str


@dataclass(frozen=True)
class TallyState:
    node_id: str
    label: str
    status: LearnerState
    evidence: tuple[EvidenceQuote, ...] = ()
    times_asked: int = 0
    last_asked_turn: int | None = None


@dataclass(frozen=True)
class Contradiction:
    """What a wrongness-bearing update collides with.

    ``reference_clause`` is the private reference wording the student's claim
    runs against; ``kind`` is a free-form short label (``"inverted
    relationship"``, ``"wrong actor"``). **``kind`` is observability-only and
    gates nothing** — the model localizes a contradiction reliably but names it
    at coin-flip, so no code vocabulary exists for it to hallucinate against.
    """

    reference_clause: str
    kind: str


@dataclass(frozen=True)
class TallyUpdate:
    node_id: str
    status: LearnerState
    evidence: EvidenceQuote | None = None
    #: Defaulted so every pre-P3.2 construction stays valid, and so nothing
    #: carries wrongness unless the producer was asked for it.
    wrongness: Wrongness = "none"
    #: Non-``None`` iff ``wrongness != "none"`` — enforced in `_decode_updates`,
    #: which a static JSON-schema ``required`` list cannot express.
    contradiction: Contradiction | None = None


@dataclass(frozen=True)
class CarriedChallenge:
    """One claim this student made about ``node_id`` in an EARLIER attempt
    (P3.2 L2c / D4 — *carry the question, never the punishment*). ``prior_quote``
    is length-capped and control-stripped by the producer (`challenge.clean_quote`)
    and rides as labelled untrusted data in the payload, never in the prompt."""

    node_id: str
    prior_quote: str


@dataclass(frozen=True)
class QuestionBudget:
    questions_asked: int
    cap: int
    #: At most ONE entry (D4). Empty ⇒ payload and system turn byte-identical.
    carried_challenges: tuple[CarriedChallenge, ...] = ()


@dataclass(frozen=True)
class UnifiedQuestionResult:
    tally_updates: tuple[TallyUpdate, ...]
    action: Literal["ask", "done"]
    target_node_id: str | None
    reply: str | None
    question: str | None
    #: True when what is served is `_fallback_public_question` — a verbatim public
    #: clause, not a question the engine wrote about the target. The caller must
    #: NOT spend one of the node's `MAX_ASKS_PER_NODE` probes on it.
    fallback_served: bool = False


def _schema(*, wrongness: bool = False) -> dict[str, Any]:
    """The response schema (`questioning/prompts`). ``wrongness=False`` is
    byte-identical to pre-P3.2; the two enums stay single-sourced here."""
    return prompts.response_schema(
        statuses=sorted(_VALID_STATES),
        wrongness_values=sorted(WRONGNESS_VALUES) if wrongness else None,
    )


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _call_unified(
    *,
    payload: dict[str, Any],
    messages: Sequence[dict[str, str]] | None = None,
    client: Any | None = None,
    wrongness: bool = False,
) -> str:
    """One questioning call.

    ``client=None`` resolves `bounded_client()`, byte-identical to the
    pre-P3.2 path. An injected client is the replay/validation seam: it must
    satisfy exactly ``client.chat.completions.create(**kwargs)`` returning an
    object with ``choices[0].message.content: str``, and nothing else about it
    is assumed.
    """
    active_client: Any = bounded_client() if client is None else client
    model = os.getenv("APOLLO_UNIFIED_QUESTION_MODEL") or _DEFAULT_MODEL
    kwargs: dict[str, Any] = {
        "model": cast(Any, model),
        "response_format": {
            "type": "json_schema",
            "json_schema": _schema(wrongness=wrongness),
        },
        "messages": (
            list(messages) if messages is not None else _base_messages(payload, wrongness=wrongness)
        ),
    }
    if _is_reasoning_model(model):
        kwargs["reasoning_effort"] = os.getenv(
            "APOLLO_UNIFIED_QUESTION_REASONING_EFFORT", _DEFAULT_REASONING_EFFORT
        )
    response = active_client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or "{}"


def _base_messages(payload: dict[str, Any], *, wrongness: bool = False) -> list[dict[str, str]]:
    """The system + user turn. The L2c prompt clause is derived FROM the payload,
    never passed alongside it, so the clause and the data it describes can never
    disagree — no carried challenge, no clause, byte-identical system turn."""
    budget = payload.get("budget")
    carried = bool(budget.get("carried_challenges")) if isinstance(budget, dict) else False
    return [
        {
            "role": "system",
            "content": prompts.build_system_prompt(wrongness=wrongness, carried=carried),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _verbatim_span(value: Any, message: str) -> str | None:
    """The STUDENT's own words for the model's quote `value`, sliced out of `message`.

    The model's rendering only has to match word-for-word ignoring case and
    punctuation (Q1 — a raw substring re-check silently dropped valid updates),
    but what gets persisted is the student's raw text: `done.py` hands these
    quotes to the transcript adjudicator as "the student said", and a cleaned-up
    rendering there would attribute words the student never typed (P1.3).
    Returns None when the quote is not the student's, which drops the update.
    """
    if not isinstance(value, str):
        return None
    wanted = [token.casefold() for token in WORD_RE.findall(value)]
    if not wanted:
        return None
    spans = list(WORD_RE.finditer(message))
    tokens = [span.group(0).casefold() for span in spans]
    for start in range(len(tokens) - len(wanted) + 1):
        if tokens[start : start + len(wanted)] == wanted:
            return message[spans[start].start() : spans[start + len(wanted) - 1].end()]
    return None


def _public_question_parts(problem_text: str) -> list[str]:
    parts = [re.sub(r"\s+", " ", part).strip(" .") for part in problem_text.split("?")]
    return [part for part in parts if part]


def _serialize_tally(tally_state: Sequence[TallyState]) -> list[dict[str, Any]]:
    return [
        {
            "node_id": item.node_id,
            "label": item.label,
            "status": item.status,
            "evidence": [evidence.__dict__ for evidence in item.evidence],
            "times_asked": item.times_asked,
            "last_asked_turn": item.last_asked_turn,
        }
        for item in tally_state
    ]


def _decode(raw: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode_contradiction(value: Any) -> Contradiction | None:
    """The `contradiction` object, or ``None`` when it is absent/malformed.

    ``kind`` is free-form and is NEVER validated against a vocabulary — only
    its type. Requiring a non-blank ``reference_clause`` is the whole check:
    without it a finding names nothing and cannot be corroborated later.
    """
    if not isinstance(value, dict):
        return None
    reference_clause = value.get("reference_clause")
    kind = value.get("kind")
    if not isinstance(reference_clause, str) or not reference_clause.strip():
        return None
    if not isinstance(kind, str):
        return None
    return Contradiction(reference_clause=reference_clause.strip(), kind=kind.strip())


def _decode_wrongness(
    item: dict[str, Any], *, node_id: str, status: str
) -> tuple[Wrongness, Contradiction | None] | None:
    """The P3.2 label for one update, or ``None`` meaning *drop this update*.

    Three rules, all of them enforced here because a static JSON-schema
    ``required`` list cannot express any of them:

    * an off-enum label coerces to ``"none"`` (logged) rather than raising —
      this feeds the grade path and must never break a turn;
    * ``status == "missing"`` forces ``"none"``: a missing node carries no
      student claim, so there is nothing for the student to contradict (and by
      construction a `contradicts_material` on `missing` can never survive the
      verbatim gate, which drops evidence-less non-missing updates only);
    * ``wrongness != "none"`` with no usable `contradiction` object drops the
      WHOLE update — a finding with nothing to point at is not a finding, and
      keeping the state change while discarding the label would silently
      launder an unsupported accusation into the ledger.
    """
    raw = item.get("wrongness", "none")
    # `isinstance` FIRST: an unhashable value (a dict or list from a
    # non-conforming model) raises `TypeError` on a bare `in frozenset` test,
    # and this decoder must be total — it runs on the grade path.
    if not isinstance(raw, str) or raw not in WRONGNESS_VALUES:
        _LOG.warning(
            "apollo_wrongness_off_enum node_id=%s value=%r",
            node_id,
            _bounded_debug_text(raw),
        )
        raw = "none"
    if status == "missing":
        raw = "none"
    if raw == "none":
        return "none", None
    contradiction = _decode_contradiction(item.get("contradiction"))
    if contradiction is None:
        _LOG.warning(
            "apollo_wrongness_missing_contradiction node_id=%s wrongness=%s",
            node_id,
            raw,
        )
        return None
    return cast(Wrongness, raw), contradiction


def _decode_updates(
    decoded: dict[str, Any],
    *,
    valid_ids: set[str],
    transcript: Sequence[tuple[str, str]],
) -> tuple[TallyUpdate, ...]:
    student_by_turn = {
        turn_id: content for turn_id, (role, content) in enumerate(transcript) if role == "student"
    }
    updates: list[TallyUpdate] = []
    items = decoded.get("tally_updates", [])
    if not isinstance(items, list):
        return ()
    for item in items:
        if not isinstance(item, dict):
            continue
        node_id = item.get("node_id")
        status = item.get("status")
        if not isinstance(node_id, str) or node_id not in valid_ids or status not in _VALID_STATES:
            continue
        evidence: EvidenceQuote | None = None
        raw_evidence = item.get("evidence")
        if isinstance(raw_evidence, dict):
            turn_id = raw_evidence.get("turn_id")
            if isinstance(turn_id, int) and not isinstance(turn_id, bool):
                quote = _verbatim_span(raw_evidence.get("quote"), student_by_turn.get(turn_id, ""))
                if quote is not None:
                    evidence = EvidenceQuote(turn_id=turn_id, quote=quote)
        if status != "missing" and evidence is None:
            # P2.4: this normalized check is the SINGLE evidence validator — the
            # controller no longer re-checks raw substrings. Log the drop so a
            # silently-lost tally update stays observable.
            _LOG.warning(
                "apollo_question_evidence_rejected node_id=%s status=%s quote=%r",
                node_id,
                status,
                _bounded_debug_text(
                    raw_evidence.get("quote") if isinstance(raw_evidence, dict) else None
                ),
            )
            continue
        # AFTER the verbatim gate on purpose: a wrongness-bearing update is by
        # definition non-`missing`, so the gate above already dropped it if its
        # span was not the student's own words. This is a rider on that gate,
        # never a second implementation of it.
        labelled = _decode_wrongness(item, node_id=node_id, status=status)
        if labelled is None:
            continue
        wrongness, contradiction = labelled
        updates.append(
            TallyUpdate(
                node_id=node_id,
                status=cast(LearnerState, status),
                evidence=evidence,
                wrongness=wrongness,
                contradiction=contradiction,
            )
        )
    return tuple(updates)


def _student_reply(decoded: dict[str, Any]) -> tuple[str, str]:
    acknowledgement = decoded.get("acknowledgement")
    question = decoded.get("question")
    ack = re.sub(r"\s+", " ", acknowledgement).strip() if isinstance(acknowledgement, str) else ""
    clean_question = re.sub(r"\s+", " ", question).strip() if isinstance(question, str) else ""
    return f"{ack} {clean_question}".strip(), clean_question


def _fallback_public_question(
    *,
    public_parts: Sequence[str],
    reference_graph: KGGraph,
    target_node_id: str | None,
) -> str:
    """A verbatim public clause standing in for the target node's question."""
    if not public_parts:
        return "?"
    index = next(
        (
            node_index
            for node_index, node in enumerate(reference_graph.nodes)
            if node.node_id == target_node_id
        ),
        0,
    )
    return f"{public_parts[min(index, len(public_parts) - 1)]}?"


def question_cap() -> int:
    raw = os.getenv("APOLLO_UNIFIED_QUESTION_CAP")
    try:
        return max(0, int(raw)) if raw is not None else _DEFAULT_QUESTION_CAP
    except (TypeError, ValueError):
        return _DEFAULT_QUESTION_CAP


def _effective_counts(
    tally_state: Sequence[TallyState], updates: Sequence[TallyUpdate]
) -> Counter[str]:
    statuses = {item.node_id: item.status for item in tally_state}
    statuses.update({item.node_id: item.status for item in updates})
    return Counter(statuses.values())


def _build_payload(
    *,
    problem_text: str,
    public_parts: Sequence[str],
    reference_graph: KGGraph,
    tally_state: Sequence[TallyState],
    budget: QuestionBudget,
    policy: SelectionPolicy,
    transcript: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """Serialize the call payload with graded nodes first and the askable set named."""
    # `carried_challenges` appears ONLY when non-empty: an always-present empty
    # list would change every level-0/1 payload byte-for-byte. `prior_quote` is
    # untrusted data in a labelled payload field, never in the system turn.
    carried = (
        {
            "carried_challenges": [
                {"node_id": item.node_id, "prior_quote": item.prior_quote}
                for item in budget.carried_challenges
            ]
        }
        if budget.carried_challenges
        else {}
    )
    return {
        "public_problem": problem_text,
        "public_question_parts": [
            {"index": index, "text": text} for index, text in enumerate(public_parts)
        ],
        "private_reference_nodes": [
            {
                "node_id": node.node_id,
                "type": node.node_type,
                "graded": is_graded(node),
                "content": node.content.model_dump(),
            }
            for node in ordered_nodes(reference_graph)
        ],
        "private_reference_edges": [edge.model_dump(mode="json") for edge in reference_graph.edges],
        "tally_state": _serialize_tally(tally_state),
        "budget": {
            "questions_asked": budget.questions_asked,
            "cap": budget.cap,
            "reserved_for_graded": policy.reserved_for_graded,
            "askable_node_ids": list(policy.askable_ids),
            **carried,
        },
        "transcript": [
            {"turn_id": turn_id, "role": role, "content": content}
            for turn_id, (role, content) in enumerate(transcript)
        ],
    }


@dataclass(frozen=True)
class _ServedReply:
    reply: str
    question: str
    target: str
    fallback_reason: FallbackReason | None
    belt_hit_served: bool
    draft_verdict: BeltVerdict
    regenerate_decoded: dict[str, Any] | None = None
    regenerate_verdict: BeltVerdict | None = None
    fallback_served: bool = False


async def _resolve_served_reply(
    *,
    decoded: dict[str, Any],
    raw: str,
    base_messages: list[dict[str, str]],
    payload: dict[str, Any],
    policy: SelectionPolicy,
    target: str,
    reference_graph: KGGraph,
    problem_text: str,
    student_messages: Sequence[str],
    public_parts: Sequence[str],
    client: Any | None = None,
    wrongness: bool = False,
) -> _ServedReply:
    """One regenerate at most, for an off-policy target and/or a malformed shape."""

    def belt(text: str) -> BeltVerdict:
        return belt_verdict(
            text,
            reference_graph=reference_graph,
            public_text=problem_text,
            student_messages=student_messages,
        )

    reply, question = _student_reply(decoded)
    verdict = belt(reply)
    off_policy = target not in policy.askable_ids
    if not off_policy and not verdict.malformed:
        return _ServedReply(reply, question, target, None, verdict.hit, verdict)

    reason: FallbackReason = "off_policy_regenerated" if off_policy else "malformed_regenerated"
    # A draft can be BOTH off-policy and malformed — send both corrections, or the
    # single regenerate is spent fixing one defect while the other still fails it.
    feedback = " ".join(
        text
        for text in (
            prompts.off_policy_feedback(policy.askable_ids) if off_policy else "",
            prompts.MALFORMED_FEEDBACK if verdict.malformed else "",
        )
        if text
    )
    regenerate_raw = await asyncio.to_thread(
        _call_unified,
        payload=payload,
        messages=[
            *base_messages,
            {"role": "assistant", "content": raw},
            {"role": "user", "content": feedback},
        ],
        client=client,
        wrongness=wrongness,
    )
    regenerate_decoded = _decode(regenerate_raw)
    reply, question = _student_reply(regenerate_decoded)
    regenerate_verdict = belt(reply)
    retried_target = regenerate_decoded.get("target_node_id")
    if isinstance(retried_target, str) and retried_target in policy.askable_ids:
        target, off_policy = retried_target, False
    if off_policy or regenerate_verdict.malformed:
        target = policy.askable_ids[0]
        reply = question = _fallback_public_question(
            public_parts=public_parts,
            reference_graph=reference_graph,
            target_node_id=target,
        )
        return _ServedReply(
            reply,
            question,
            target,
            "off_policy_exhausted" if off_policy else "malformed_exhausted",
            False,
            verdict,
            regenerate_decoded,
            regenerate_verdict,
            fallback_served=True,
        )
    return _ServedReply(
        reply,
        question,
        target,
        reason,
        regenerate_verdict.hit,
        verdict,
        regenerate_decoded,
        regenerate_verdict,
    )


async def evaluate_and_ask(
    *,
    transcript: Sequence[tuple[str, str]],
    reference_graph: KGGraph,
    problem: Any,
    tally_state: Sequence[TallyState],
    budget: QuestionBudget,
    client: Any | None = None,
    wrongness: bool = False,
    contested_ids: Collection[str] = (),
    contested_quotes: Mapping[str, str] | None = None,
    challenge_gate: bool = False,
) -> UnifiedQuestionResult:
    """Apply the hard budget and the selection policy around one call (+ ≤1 regenerate).

    ``client`` is the injectable seam (default ``None`` ⇒ `bounded_client()`),
    used by the turn-replay harness. ``wrongness`` turns the P3.2 producer on;
    it is threaded from the caller's `APOLLO_WRONGNESS_LEVEL >= 1` decision, and
    with it off the schema and the system turn are byte-identical to pre-P3.2.

    The three level-2 inputs are all defaulted-off and all resolved by the
    controller from ONE ledger read: ``contested_ids`` reorders the askable set
    (L2a), ``contested_quotes`` maps a graded node to its latest material
    contradiction, and ``challenge_gate`` arms the done-gate (L2b) that spends
    them. At level 0 and 1 all three are inert and this function behaves exactly
    as it did before P3.2.
    """
    if budget.questions_asked >= budget.cap:
        # PRESERVED VERBATIM (R2): this branch returns before `_call_unified`,
        # so the final turn's tally updates — wrongness included — are never
        # produced at all. That is a pre-existing latent defect; it is LOGGED so
        # the campaign can size the loss, and deliberately NOT changed here.
        _LOG.info(
            "apollo_tally_updates_discarded attempt_budget=%s/%s reason=budget_exhausted",
            budget.questions_asked,
            budget.cap,
        )
        _log_decision(
            tally_counts=_effective_counts(tally_state, ()),
            action="done",
            target=None,
            budget=budget,
            policy=None,
            fallback_reason="budget_exhausted",
            belt_hit_served=False,
            repeated_question_served=False,
        )
        return UnifiedQuestionResult((), "done", None, None, None)

    problem_text = str(problem.problem_text)
    public_parts = _public_question_parts(problem_text)
    student_messages = [content for role, content in transcript if role == "student"]
    valid_ids = {node.node_id for node in reference_graph.nodes}
    payload = _build_payload(
        problem_text=problem_text,
        public_parts=public_parts,
        reference_graph=reference_graph,
        tally_state=tally_state,
        budget=budget,
        policy=build_selection_policy(
            reference_graph=reference_graph,
            tally_state=tally_state,
            questions_asked=budget.questions_asked,
            cap=budget.cap,
            contested_ids=contested_ids,
        ),
        transcript=transcript,
    )
    base_messages = _base_messages(payload, wrongness=wrongness)
    raw = await asyncio.to_thread(
        _call_unified,
        payload=payload,
        messages=base_messages,
        client=client,
        wrongness=wrongness,
    )
    decoded = _decode(raw)
    updates = _decode_updates(decoded, valid_ids=valid_ids, transcript=transcript)
    # Re-resolve after this turn's updates: a node just understood stops being a
    # target, and an emptied askable set IS the code-enforced done condition.
    policy = build_selection_policy(
        reference_graph=reference_graph,
        tally_state=tally_state,
        updates=updates,
        questions_asked=budget.questions_asked,
        cap=budget.cap,
        contested_ids=contested_ids,
    )

    # L2b — the done-gate, deliberately BEFORE the done early return and AFTER
    # the policy re-resolve. `challenge.resolve` owns every guard (level, budget,
    # cap, self-declared-done) so this call site cannot forget one.
    served_challenge = challenge.resolve(
        armed=challenge_gate,
        self_declared_done=decoded.get("action") == "done",
        policy=policy,
        tally_state=tally_state,
        updates=updates,
        transcript=transcript,
        contested_quotes=contested_quotes or {},
        reference_graph=reference_graph,
        problem_text=problem_text,
        student_messages=student_messages,
        questions_asked=budget.questions_asked,
        cap=budget.cap,
    )
    if served_challenge is not None:
        reply = served_challenge.reply
        _log_decision(
            tally_counts=_effective_counts(tally_state, updates),
            action="ask",
            target=served_challenge.node_id,
            budget=budget,
            policy=policy,
            fallback_reason=None,
            belt_hit_served=False,
            repeated_question_served=False,
        )
        return UnifiedQuestionResult(updates, "ask", served_challenge.node_id, reply, reply)

    if decoded.get("action") == "done" or not policy.askable_ids:
        _log_decision(
            tally_counts=_effective_counts(tally_state, updates),
            action="done",
            target=None,
            budget=budget,
            policy=policy,
            fallback_reason=None if decoded.get("action") == "done" else "no_probeable_node",
            belt_hit_served=False,
            repeated_question_served=False,
        )
        _log_debug_cycle(decoded, None, None, None, None)
        return UnifiedQuestionResult(updates, "done", None, None, None)

    requested_target = decoded.get("target_node_id")
    served = await _resolve_served_reply(
        decoded=decoded,
        raw=raw,
        base_messages=base_messages,
        payload=payload,
        policy=policy,
        target=(
            requested_target
            if isinstance(requested_target, str) and requested_target in valid_ids
            else policy.askable_ids[0]
        ),
        reference_graph=reference_graph,
        problem_text=problem_text,
        student_messages=student_messages,
        public_parts=public_parts,
        client=client,
        wrongness=wrongness,
    )
    prior_questions = {normalized(item) for item in _transcript_questions(transcript)}
    _log_decision(
        tally_counts=_effective_counts(tally_state, updates),
        action="ask",
        target=served.target,
        budget=budget,
        policy=policy,
        fallback_reason=served.fallback_reason,
        belt_hit_served=served.belt_hit_served,
        repeated_question_served=normalized(served.question) in prior_questions,
    )
    _log_debug_cycle(
        decoded,
        served.draft_verdict,
        served.regenerate_decoded,
        served.regenerate_verdict,
        served.reply,
    )
    return UnifiedQuestionResult(
        updates,
        "ask",
        served.target,
        served.reply,
        served.question,
        fallback_served=served.fallback_served,
    )


def _transcript_questions(transcript: Sequence[tuple[str, str]]) -> list[str]:
    questions: list[str] = []
    for role, content in transcript:
        cleaned = re.sub(r"\s+", " ", content).strip()
        if role != "apollo" or not cleaned.endswith("?"):
            continue
        boundary = max(cleaned.rfind(mark, 0, len(cleaned) - 1) for mark in ".!?")
        questions.append(cleaned[boundary + 1 :].strip() if boundary >= 0 else cleaned)
    return questions


def _debug_log_enabled() -> bool:
    return os.getenv("APOLLO_UNIFIED_QUESTION_DEBUG_LOG", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _bounded_debug_text(value: Any) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip()[:300]


def _log_debug_cycle(
    draft: dict[str, Any],
    draft_verdict: BeltVerdict | None,
    regenerate: dict[str, Any] | None,
    regenerate_verdict: BeltVerdict | None,
    final: str | None,
) -> None:
    if not _debug_log_enabled():
        return
    _LOG.info(
        "apollo_unified_question_debug draft=%r belt_hit=%s belt_atoms=%s "
        "regenerate=%r regenerate_belt_hit=%s regenerate_atoms=%s final=%r",
        _bounded_debug_text(_student_reply(draft)[0]),
        draft_verdict.hit if draft_verdict is not None else None,
        draft_verdict.offending_atoms if draft_verdict is not None else (),
        _bounded_debug_text(_student_reply(regenerate)[0]) if regenerate is not None else None,
        regenerate_verdict.hit if regenerate_verdict is not None else None,
        regenerate_verdict.offending_atoms if regenerate_verdict is not None else (),
        _bounded_debug_text(final),
    )


def _log_decision(
    *,
    tally_counts: Counter[str],
    action: str,
    target: str | None,
    budget: QuestionBudget,
    policy: SelectionPolicy | None,
    fallback_reason: FallbackReason | None,
    belt_hit_served: bool,
    repeated_question_served: bool,
) -> None:
    _LOG.info(
        "apollo_unified_question_decision model=%s action=%s target=%s tally=%s "
        "budget=%s/%s graded_open=%s/%s reserved_for_graded=%s graded_only=%s askable=%s "
        "fallback_reason=%s belt_hit_served=%s repeated_question_served=%s",
        os.getenv("APOLLO_UNIFIED_QUESTION_MODEL") or _DEFAULT_MODEL,
        action,
        target,
        dict(sorted(tally_counts.items())),
        budget.questions_asked,
        budget.cap,
        policy.open_graded_topics if policy is not None else None,
        policy.graded_topic_total if policy is not None else None,
        policy.reserved_for_graded if policy is not None else None,
        policy.graded_only if policy is not None else None,
        len(policy.askable_ids) if policy is not None else None,
        fallback_reason,
        belt_hit_served,
        repeated_question_served,
    )


__all__ = [
    "WRONGNESS_VALUES",
    "CarriedChallenge",
    "Contradiction",
    "EvidenceQuote",
    "QuestionBudget",
    "TallyState",
    "TallyUpdate",
    "UnifiedQuestionResult",
    "Wrongness",
    "evaluate_and_ask",
    "question_cap",
]
