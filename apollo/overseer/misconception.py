"""Misconception inference + verifier (Class 2 Phase 2, Apollo Gap B).

Pipeline (per the research synthesis in
`.feller/tasks/2026-05-04-class-2-apollo-redesign/research/02-misconceptions.md`):

    sufficient KG?
        |---yes---> skip (no probe needed)
        |---no----> generate candidate (cheap LLM, MISTAKE-style)
                    |
                    v
                  embed candidate
                    |
                    v
                  match_by_embedding (bank, concept_id) -> top-3
                    |
                    v
                  verify (cheap LLM, Macina-style)
                    |
                    v
                  threshold:
                    score < TAU_PROBE   -> default (silent)
                    TAU_PROBE <= score < TAU_FIRE  -> probe
                    score >= TAU_FIRE  -> socratic
                    |
                    v
                  PROBE-then-confirm:
                    socratic only if previous turn already detected
                    the same bank_id; otherwise demote to probe.

Subject-agnostic by contract: every call site passes `concept_id: int`
(the FK into `apollo_concepts`). No subject/concept slug ever crosses
the boundary. The user's mandate — "0 hard coded stuff about subject
specific things" — is enforced by a signature-introspection test.

The candidate `description` and authored `bank_id` are returned for
analytics on `MisconceptionSignal` but MUST NEVER be rendered to the
student. Output filter (P2.6) blocks both verbatim. Persona shift (P2.5)
only consumes the authored `probe` / `rt_steps` payload.

Research anchors:
- FiDeLiS halt (arXiv 2405.13873) — skip-on-sufficient gate.
- MISTAKE inference (arXiv 2510.11502) — generator pattern.
- Macina verify-then-generate (arXiv 2407.09136) — separate verifier.
- Reasoning Trajectories (arXiv 2511.00371) — invisible persona shift.
- "When Verification Hurts" (arXiv 2603.27076) — TAU_PROBE soft band.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.agent._llm import cheap_chat
from apollo.ontology.nodes import Node
from apollo.overseer.misconception_bank import (
    MisconceptionEntry,
    match_by_embedding,
)
from apollo.solver.sufficiency import SufficiencyVerdict

_LOG = logging.getLogger(__name__)

# Two-tier thresholds. Picked to favor specificity at fire-time
# (TAU_FIRE high) while still surfacing a soft probe in the band where
# the verifier is less confident. Calibrate on P2.4 corpus before flag-on.
TAU_PROBE: float = 0.5
TAU_FIRE: float = 0.75

MisconceptionState = Literal["default", "probe", "socratic"]


@dataclass(frozen=True)
class MisconceptionSignal:
    """Per-turn output of the inference pipeline. Frozen value object."""

    fired: bool
    state: MisconceptionState
    # Internal-only fields. Output filter (P2.6) blocks these from leaking
    # into student-visible drafts. Carried for offline eval and analytics.
    description: str | None = None
    confusion_pair: tuple[str, str] | None = None
    bank_id: str | None = None
    bank_code: str | None = None
    # Authored payload — safe to feed into Apollo's persona shift suffix.
    probe: str | None = None
    rt_steps: tuple[str, ...] | None = None
    # Verifier-reported confidence in [0, 1].
    confidence: float = 0.0
    # Short rationale string for diagnostic logging. Never rendered.
    evidence: str = ""

    @classmethod
    def default(cls, *, evidence: str = "") -> "MisconceptionSignal":
        return cls(fired=False, state="default", evidence=evidence)


# ---- Generator (cheap LLM, MISTAKE-style) -----------------------------------

_GENERATOR_SYSTEM_PROMPT = """You are diagnosing a student who is teaching a
confused tutor (Apollo) about a STEM concept. Your job is to listen to the
student's most recent utterance and spot a single suspected misconception.

You will receive:
- the student's most recent utterance,
- a structured list of parsed claims (equations, conditions, definitions,
  variable mappings) that the student has taught Apollo so far this turn,
- a hint about what variables/premises Apollo still needs to be taught.

Output ONLY a JSON object of the form:
{"misconception": <string or null>, "evidence": <string or null>}

Rules:
1. `misconception` is a single declarative sentence (under 25 words)
   describing ONE suspected error in the student's reasoning. If the
   utterance is fine, return null.
2. Do NOT name the student. Use "the student" / "they".
3. Do NOT name the concept being taught (no "Bernoulli", "continuity",
   etc.). Describe the error in operational terms.
4. `evidence` is the shortest substring of the utterance that grounds the
   call. Null when misconception is null.
5. Bias toward null on uncertain cases — the verifier downstream is
   stricter than you.
"""


def _default_generator(
    *,
    utterance: str,
    parsed_nodes: list[Node],
    next_premise_hint: str | None,
) -> tuple[str | None, str]:
    """Run the MISTAKE-style cheap call. Returns (description, evidence)."""
    nodes_payload = [
        {
            "node_type": n.node_type,
            "content": n.content.model_dump() if hasattr(n.content, "model_dump")
                       else n.content,
            "parser_confidence": getattr(n, "parser_confidence", 1.0),
        }
        for n in parsed_nodes
    ]
    payload = {
        "utterance": utterance,
        "parsed_nodes": nodes_payload,
        "next_premise_hint": next_premise_hint,
    }
    try:
        raw = cheap_chat(
            purpose="misconception_generate",
            messages=[
                {"role": "system", "content": _GENERATOR_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("misconception generator soft-fail: %s", exc)
        return None, "generator-error"

    desc = parsed.get("misconception")
    evidence = parsed.get("evidence") or ""
    if not desc or not isinstance(desc, str) or not desc.strip():
        return None, str(evidence)
    return desc.strip(), str(evidence)


# ---- Verifier (cheap LLM, Macina-style) -------------------------------------

_VERIFIER_SYSTEM_PROMPT = """You audit whether a proposed misconception is
ACTUALLY present in a student's utterance.

You will receive:
- the student's utterance,
- their parsed claims for this turn,
- a candidate misconception description (proposed by an upstream model).

Decide whether the candidate misconception is genuinely supported by the
utterance and parsed claims. Be strict — false positives are worse than
false negatives here.

Return ONLY a JSON object of the form:
{"present": <bool>, "score": <float in [0, 1]>, "reason": <string>}

Score 0.0 = certain absent, 0.5 = ambiguous, 1.0 = certain present.
"""


def _default_verifier(
    *,
    utterance: str,
    parsed_nodes: list[Node],
    candidate_description: str,
) -> tuple[float, str]:
    """Macina-style verifier. Returns (score, reason)."""
    nodes_payload = [
        {
            "node_type": n.node_type,
            "content": n.content.model_dump() if hasattr(n.content, "model_dump")
                       else n.content,
        }
        for n in parsed_nodes
    ]
    payload = {
        "utterance": utterance,
        "parsed_nodes": nodes_payload,
        "candidate": candidate_description,
    }
    try:
        raw = cheap_chat(
            purpose="misconception_verify",
            messages=[
                {"role": "system", "content": _VERIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("misconception verifier soft-fail: %s", exc)
        return 0.0, f"verifier-error: {exc}"

    score = parsed.get("score", 0.0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    reason = str(parsed.get("reason") or "")
    return score, reason


# ---- Embedder ---------------------------------------------------------------

def _default_embedder(text: str) -> list[float]:
    """Default embedder — uses the project-wide text-embedding-3-large path.

    Imported lazily so test runs that monkeypatch the embedder via DI
    don't need OpenAI configured.
    """
    from indexing.document_embedder import embed_text
    return embed_text(text)


# ---- Dependency-injection Protocols (tests inject stubs) --------------------

class GeneratorFn(Protocol):
    def __call__(
        self,
        *,
        utterance: str,
        parsed_nodes: list[Node],
        next_premise_hint: str | None,
    ) -> tuple[str | None, str]: ...


class VerifierFn(Protocol):
    def __call__(
        self,
        *,
        utterance: str,
        parsed_nodes: list[Node],
        candidate_description: str,
    ) -> tuple[float, str]: ...


class EmbedderFn(Protocol):
    def __call__(self, text: str) -> list[float]: ...


class RetrieverFn(Protocol):
    """Pgvector retrieval, abstracted so tests can inject a stub bank.

    Default implementation calls `match_by_embedding` against the live
    DB. Tests inject a closure over an in-memory list.
    """

    async def __call__(
        self,
        *,
        concept_id: int,
        query_embedding: list[float],
        k: int,
    ) -> list[tuple[MisconceptionEntry, float]]: ...


# ---- Main entry point -------------------------------------------------------

async def infer_misconception(
    *,
    db: AsyncSession | None,
    concept_id: int,
    utterance: str,
    parsed_nodes: list[Node],
    sufficiency: SufficiencyVerdict | None,
    previous_signals: tuple[MisconceptionSignal, ...] = (),
    generator: GeneratorFn | None = None,
    verifier: VerifierFn | None = None,
    embedder: EmbedderFn | None = None,
    retriever: RetrieverFn | None = None,
) -> MisconceptionSignal:
    """Run the misconception-inference pipeline for one chat turn.

    Subject-agnostic: takes `concept_id` (DB FK) only. Never sees a
    subject or concept slug.

    Args:
        db: Async session for the bank retrieval (pgvector index).
        concept_id: FK into apollo_concepts. Resolved upstream from
            apollo_sessions.concept_id.
        utterance: Student's most recent message text.
        parsed_nodes: Typed Node list emitted by the parser this turn.
        sufficiency: Pre-computed SufficiencyVerdict for this turn. When
            state == "sufficient", the pipeline short-circuits — there
            is no productive misconception to probe.
        previous_signals: The last 1-2 signals from earlier turns in the
            same session. Used by the PROBE-then-confirm gate to require
            corroboration before escalating to socratic.
        generator, verifier, embedder, retriever: Test-time DI hooks.
            Default implementations call OpenAI / pgvector. Pass `db=None`
            with a stub `retriever` to run the pipeline against an
            in-memory bank in unit tests.

    Returns:
        MisconceptionSignal — `fired=False, state="default"` on the
        no-misconception path; otherwise the strongest matched candidate.
    """
    # Stage 1: Skip if KG already entails the target.
    if sufficiency is not None and sufficiency.state == "sufficient":
        return MisconceptionSignal.default(evidence="skip:sufficient")

    # Stage 2: Master kill-switch via env flag (default off until P2.4 corpus
    # validates recall/specificity targets). Implementation runs even when
    # disabled so tests of the inference path don't depend on the flag — the
    # callsite (chat.py, P2.7) decides whether to feed the signal into the
    # persona shift.
    gen = generator or _default_generator
    ver = verifier or _default_verifier
    emb = embedder or _default_embedder

    description, gen_evidence = gen(
        utterance=utterance,
        parsed_nodes=parsed_nodes,
        next_premise_hint=(
            sufficiency.next_premise_hint if sufficiency is not None else None
        ),
    )
    if not description:
        return MisconceptionSignal.default(evidence="generator:none")

    # Stage 3: Embed the candidate and retrieve from the bank.
    try:
        query_embedding = emb(description)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("misconception embedder soft-fail: %s", exc)
        query_embedding = []

    matches: list[tuple[MisconceptionEntry, float]] = []
    if retriever is not None:
        try:
            matches = await retriever(
                concept_id=concept_id,
                query_embedding=query_embedding,
                k=3,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("misconception retriever soft-fail: %s", exc)
            matches = []
    elif db is not None and query_embedding:
        try:
            matches = await match_by_embedding(
                db,
                concept_id=concept_id,
                query_embedding=query_embedding,
                k=3,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("misconception bank retrieval soft-fail: %s", exc)
            matches = []

    if not matches:
        # No bank entry matched — record candidate but do not fire.
        # The candidate description is internal-only; it never reaches
        # the student.
        return MisconceptionSignal(
            fired=False,
            state="default",
            description=description,
            confidence=0.0,
            evidence=f"no-bank-match (gen: {gen_evidence})",
        )

    # Stage 4: Verify the top match.
    top_entry, top_similarity = matches[0]
    verifier_score, verifier_reason = ver(
        utterance=utterance,
        parsed_nodes=parsed_nodes,
        candidate_description=top_entry.description,
    )

    # Combined score: geometric mean of retrieval similarity and verifier
    # score. Both must agree to push the call up. Clamp similarity to
    # [0, 1] (cosine on unit-norm embeddings is in [-1, 1] but for text
    # embeddings near-orthogonal pairs are effectively 0).
    sim_norm = max(0.0, min(1.0, top_similarity))
    combined = (sim_norm * verifier_score) ** 0.5

    # Stage 5: Threshold.
    if combined < TAU_PROBE:
        return MisconceptionSignal(
            fired=False,
            state="default",
            description=description,
            bank_id=str(top_entry.id),
            bank_code=top_entry.code,
            confidence=combined,
            evidence=f"below-tau-probe ({combined:.2f})",
        )

    state: MisconceptionState
    if combined >= TAU_FIRE:
        state = "socratic"
    else:
        state = "probe"

    # Stage 6: PROBE-then-confirm. First detection on a turn cannot fire
    # socratic without a corroborating prior detection of the same
    # bank entry within the previous 1-2 signals.
    if state == "socratic":
        prior_codes = {
            s.bank_code for s in previous_signals
            if s.bank_code is not None and s.fired
        }
        if top_entry.code not in prior_codes:
            state = "probe"

    confusion_pair = top_entry.confusion_pair
    return MisconceptionSignal(
        fired=True,
        state=state,
        description=top_entry.description,
        confusion_pair=confusion_pair,
        bank_id=str(top_entry.id),
        bank_code=top_entry.code,
        probe=top_entry.probe_question,
        rt_steps=top_entry.rt_steps,
        confidence=combined,
        evidence=(
            f"sim={sim_norm:.2f} verifier={verifier_score:.2f} "
            f"reason={verifier_reason}"
        ),
    )


def summarize_for_rubric(
    signals: list[MisconceptionSignal],
    *,
    resolved_window: int = 2,
) -> dict[str, float]:
    """Reduce a sequence of per-turn signals into a per-bank-code score
    map for the misconception axis (P2.8 / `apollo.overseer.rubric`).

    The signals must be in turn order (oldest first). For each
    `bank_code` that fired at any turn:
        score = 1.0 if no firing on the last `resolved_window` turns
                    of the attempt — interpreted as "resolved",
        score = 0.5 otherwise — "detected but unresolved".

    Codes that never fired are excluded (no penalty, no bonus).

    The resolved-window default of 2 matches the plan: the last two
    chat turns must be clean of the same code for the misconception
    to count as resolved.
    """
    fired_codes: set[str] = set()
    for s in signals:
        if s.fired and s.bank_code:
            fired_codes.add(s.bank_code)

    if not fired_codes:
        return {}

    # The last `resolved_window` signals (oldest-first list, slice tail).
    tail = signals[-resolved_window:] if signals else []
    tail_codes = {s.bank_code for s in tail if s.fired and s.bank_code}

    out: dict[str, float] = {}
    for code in fired_codes:
        out[code] = 0.5 if code in tail_codes else 1.0
    return out


def is_enabled() -> bool:
    """Master gate consulted by the chat handler (P2.7). Exposed here so
    the inference module owns its own flag — callers do not duplicate
    string literals."""
    return os.getenv("APOLLO_MISCONCEPTION_ENABLED", "").lower() in {
        "1", "true", "yes", "on"
    }


__all__ = [
    "TAU_PROBE",
    "TAU_FIRE",
    "MisconceptionState",
    "MisconceptionSignal",
    "infer_misconception",
    "summarize_for_rubric",
    "is_enabled",
    "GeneratorFn",
    "VerifierFn",
    "EmbedderFn",
    "RetrieverFn",
]
