"""Persona brief schema (Task D2).

A "persona" is an agent-student's teaching script for ONE attempt against
ONE (subject, concept, problem) triple, plus the EXPECTED LEDGER OUTCOME that
attempt should produce once graded. The four archetypes (spec §5 / plan D2):

- ``strong``               — teaches every reference node correctly.
- ``partial``               — teaches a subset correctly and silently omits
  the rest (no ambiguous utterance for the omitted nodes — just never said).
- ``misconception``         — teaches most nodes correctly but asserts one
  bank-listed wrong belief instead of the node it opposes.
- ``vague_then_clarifies``  — teaches most nodes correctly but is
  deliberately non-committal on one node, forcing Apollo's clarification
  loop; ``clarification_policy`` decides whether the persona resolves it
  (``answer_correctly``) or continues to dodge (``stay_vague``) or asserts
  the wrong thing when pressed (``answer_wrong``).

``PersonaAttempt.expected`` is authored by hand against the REAL reference
graph (validated by ``validate.py``/the D2 test), so S3/S4 stage audits
(``campaign/judges/``) have a per-attempt ground truth to diff the actual
ledger against.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "PERSONA_ARCHETYPES",
    "CLARIFICATION_POLICIES",
    "ExpectedLedger",
    "PersonaAttempt",
]

PERSONA_ARCHETYPES = ("strong", "partial", "misconception", "vague_then_clarifies")
CLARIFICATION_POLICIES = ("answer_correctly", "answer_wrong", "stay_vague")

PersonaArchetype = Literal["strong", "partial", "misconception", "vague_then_clarifies"]
ClarificationPolicy = Literal["answer_correctly", "answer_wrong", "stay_vague"]


class ExpectedLedger(BaseModel):
    """The ledger outcome a persona attempt is authored to produce.

    ``credited``/``unresolved``/``misconceptions`` hold reference
    ``canonical_key``s (``eq.*`` / ``cond.*`` / ``simp.*`` / ``def.*`` /
    ``proc.*`` for nodes, ``misc.*`` for misconceptions) — see
    ``apollo/subjects/AUTHORING.md`` for the ``entity_key`` convention this
    mirrors 1:1. A key must appear in at most one of ``credited``/
    ``unresolved`` (a node can't be simultaneously taught-well and omitted).
    """

    credited: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    misconceptions: list[str] = Field(default_factory=list)
    expects_clarification: bool = False

    @model_validator(mode="after")
    def _credited_and_unresolved_disjoint(self) -> ExpectedLedger:
        overlap = set(self.credited) & set(self.unresolved)
        if overlap:
            raise ValueError(f"keys cannot be both credited and unresolved: {sorted(overlap)}")
        return self

    def to_ledger_dict(self) -> dict[str, list[str]]:
        """Convert to the dict shape ``campaign.judges.s3_student_fidelity.
        ledger_vs_expected`` consumes as its ``expected`` argument: a plain
        dict with ``credited``/``unresolved``/``misconceptions`` list-of-str
        keys (S3 reads exactly these three, nothing else)."""
        return {
            "credited": list(self.credited),
            "unresolved": list(self.unresolved),
            "misconceptions": list(self.misconceptions),
        }


class PersonaAttempt(BaseModel):
    """One authored campaign attempt: an agent-student's teaching script for
    a specific (subject, concept, problem) plus its expected ledger."""

    persona: PersonaArchetype
    subject: str
    concept: str
    problem_id: str
    system_prompt: str
    scripted_beats: list[str] = Field(min_length=1)
    clarification_policy: ClarificationPolicy
    expected: ExpectedLedger

    @model_validator(mode="after")
    def _vague_persona_expects_clarification(self) -> PersonaAttempt:
        if self.persona == "vague_then_clarifies" and not self.expected.expects_clarification:
            raise ValueError(
                "vague_then_clarifies personas must set expected.expects_clarification=True"
            )
        return self
