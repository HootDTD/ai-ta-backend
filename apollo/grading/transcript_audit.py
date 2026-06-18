"""WU-4B1 §6.4 step 12 / §6.3 — the Done-time batched transcript audit.

ONE batched ``main_chat`` call over the simulator-flagged ``missing_node``
reference entities + the raw transcript. Each entity comes back with a
supporting span or ``null``. A found span upgrades the ``missing_node`` finding
to a covered-grade finding at confidence ``<= 0.75`` (the same cap as the
``llm`` resolution tier — NEVER the ``alias`` tier 0.92, anti-laundering) and
emits an :class:`AliasCandidate` value object. A ``null`` leaves the
``missing_node`` finding intact.

Any audit-infrastructure failure (timeout / error / JSON-parse failure / empty
payload when entities were asked) surfaces as
:class:`apollo.errors.TranscriptAuditUnavailableError` — **never** "skip the
audit and emit the missing finding". This module does NOT catch that error; the
orchestrator (:func:`apollo.grading.audited_grade.build_audited_grade`) catches
it at the audit boundary and routes it to the suppress-ALL-``missing``
abstention gate.

``main_chat`` is REUSED from ``apollo.agent._llm`` (consume-only). Tests patch
``apollo.grading.transcript_audit.main_chat`` so no live OpenAI call ever fires;
the live path is reachable only when the caller passes :func:`main_chat_auditor`
explicitly (the default ``audit_fn``). Mirrors
``apollo.resolution.adjudication``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from apollo.agent._llm import main_chat
from apollo.errors import TranscriptAuditUnavailableError

_LOG = logging.getLogger(__name__)

# The audit-upgrade confidence cap. Equals METHOD_CONFIDENCE_CAP["llm"] (0.75) —
# a NAMED constant in the grading package, NOT a key added to the frozen
# resolution METHOD_CONFIDENCE_CAP map (RECON correction). An audit-found span
# resolves at THIS cap, never the alias tier (0.92), until a teacher approves
# the learned alias (§6.3/§8 anti-laundering).
TRANSCRIPT_AUDIT_CONFIDENCE_CAP: float = 0.75
TRANSCRIPT_AUDIT_METHOD: str = "transcript_audit"

# §6.3 "Context budget": the transcript is chunked into character windows so a
# very long Done transcript cannot blow the model's context. Entities are
# re-asked per chunk and a span found in ANY chunk wins (spans deduped). A short
# transcript is a single chunk = a single audit_fn call (tests rely on this).
AUDIT_TRANSCRIPT_CHAR_BUDGET: int = 8000

_RESPONSE_FORMAT = {"type": "json_object"}
_PURPOSE = "transcript_audit"


@dataclass(frozen=True)
class MissingEntity:
    """A simulator-flagged missing reference entity, fed to the batched audit."""

    canonical_key: str
    display_name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class AliasCandidate:
    """A span-derived learned-alias candidate (anti-laundering, §6.3/§8).

    Value object ONLY — WU-3B2 owns the teacher-approval queue table; emitted
    here, persisted nowhere by this unit. Resolves at the transcript-audit cap
    (0.75), NEVER the alias tier (0.92), until a teacher approves it."""

    canonical_key: str
    span: str
    confidence: float = TRANSCRIPT_AUDIT_CONFIDENCE_CAP


@dataclass(frozen=True)
class AuditResult:
    """Outcome of ONE batched transcript audit over the missing entities."""

    upgraded_keys: frozenset[str]
    spans_by_key: Mapping[str, str]
    alias_candidates: tuple[AliasCandidate, ...]


@dataclass(frozen=True)
class AuditRequest:
    """The single batched audit request: the missing entities + (a chunk of) the
    transcript. Frozen so an injected ``audit_fn`` can be a pure function."""

    entities: tuple[MissingEntity, ...]
    transcript: str


# An auditor maps a request to ``{canonical_key: span_or_None}`` for the entities
# it was asked about. A None span = "not found"; a non-None span = "found".
AuditReply = dict[str, str | None]
AuditFn = Callable[[AuditRequest], AuditReply]


def _build_messages(request: AuditRequest) -> list[dict[str, str]]:
    entity_lines = "\n".join(
        f"- {e.canonical_key}: {e.display_name}"
        + (f" (aka {', '.join(e.aliases)})" if e.aliases else "")
        for e in request.entities
    )
    system = (
        "You audit a teaching transcript for concepts a parser may have missed. "
        "For EACH listed concept key, return the SHORTEST verbatim quote from "
        "the transcript that demonstrates the student taught it, or null if the "
        "student never taught it. Return STRICT JSON "
        '{"spans": {"<canonical_key>": "<quote>" | null}}. Never invent a quote; '
        "quote only text that literally appears in the transcript."
    )
    user = (
        f"Concept keys to audit:\n{entity_lines}\n\n"
        f"Transcript:\n{request.transcript}\n\n"
        'Respond with {"spans": {...}} only.'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def main_chat_auditor(request: AuditRequest) -> AuditReply:
    """The real one-call auditor: a single ``main_chat`` (main model,
    temperature 0) returning strict JSON. Surfaces any transient failure (incl.
    ``json.JSONDecodeError``) as ``TranscriptAuditUnavailableError`` (NO
    FALLBACK); re-raises an already-named ``TranscriptAuditUnavailableError``
    verbatim. Mirrors ``main_chat_adjudicator``."""
    try:
        raw = main_chat(
            purpose=_PURPOSE,
            messages=_build_messages(request),
            response_format=_RESPONSE_FORMAT,
            temperature=0.0,
        )
        parsed = json.loads(raw or "{}")
        spans = parsed.get("spans", {})
        reply: AuditReply = {}
        for key, span in spans.items():
            reply[str(key)] = None if span is None else str(span)
        return reply
    except TranscriptAuditUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface as a named infra error
        raise TranscriptAuditUnavailableError(last_error=str(exc)) from exc


def _chunk_transcript(transcript: str) -> tuple[str, ...]:
    """Split the transcript into character windows no larger than the budget.

    A transcript at or under the budget is a single chunk (one ``audit_fn``
    call). An empty transcript still yields one (empty) chunk so the audit is
    always asked once when entities are present."""
    if len(transcript) <= AUDIT_TRANSCRIPT_CHAR_BUDGET:
        return (transcript,)
    budget = AUDIT_TRANSCRIPT_CHAR_BUDGET
    return tuple(transcript[i : i + budget] for i in range(0, len(transcript), budget))


def audit_missing(
    missing_entities: tuple[MissingEntity, ...],
    transcript: str,
    *,
    audit_fn: AuditFn | None = None,
) -> AuditResult:
    """Run ONE batched audit (never per-entity) over the missing entities.

    Empty ``missing_entities`` -> an empty :class:`AuditResult` and NO call
    (mirrors ``adjudicate``'s empty-remainder short circuit). A returned key not
    in the asked set is ignored (defensive, logged). A ``None`` span = "not
    found" (entity stays missing). A non-None span = "found" -> the key lands in
    ``upgraded_keys`` + ``spans_by_key`` + an :class:`AliasCandidate` at
    ``TRANSCRIPT_AUDIT_CONFIDENCE_CAP``.

    A long transcript is chunked (entities re-asked per chunk; a span found in
    ANY chunk wins; the FIRST span found for a key is kept). Any infra failure
    propagates as ``TranscriptAuditUnavailableError`` (raised by
    ``main_chat_auditor``, or re-raised verbatim from a custom ``audit_fn`` that
    raises the named error) — this function does NOT catch it."""
    if not missing_entities:
        return AuditResult(upgraded_keys=frozenset(), spans_by_key={}, alias_candidates=())

    fn = audit_fn if audit_fn is not None else main_chat_auditor
    asked = {e.canonical_key for e in missing_entities}

    spans_by_key: dict[str, str] = {}
    for chunk in _chunk_transcript(transcript):
        request = AuditRequest(entities=missing_entities, transcript=chunk)
        reply = fn(request)
        for key, span in reply.items():
            if key not in asked:
                _LOG.info("transcript_audit_unasked_key returned=%s", key)
                continue
            if span is None or key in spans_by_key:
                continue
            spans_by_key[key] = span

    # Deterministic ordering: alias candidates by canonical_key.
    upgraded = tuple(sorted(spans_by_key))
    alias_candidates = tuple(
        AliasCandidate(canonical_key=key, span=spans_by_key[key]) for key in upgraded
    )
    return AuditResult(
        upgraded_keys=frozenset(upgraded),
        spans_by_key=dict(spans_by_key),
        alias_candidates=alias_candidates,
    )
