"""S4 — Apollo coherence audit (spec §4 table, row 4).

Item = one sampled full session: the confused-learner's questions and
clarification follow-ups, the node ledger's final unresolved/misconceived
keys, and the clarification trace (questions asked, answers, credit
granted/denied). Checks whether Apollo's questions actually targeted what the
ledger later marks unresolved/misconceived, and whether grading honored
clarification resolutions (a clarification that earned credit must show up as
a ``credited``/``clarification``-method ledger entry, not a lingering
unresolved one).

Gate (E3): coherent on >=90% of sampled sessions (a session-level bar, not a
per-utterance one — one item per session).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from campaign.judges.base import StageJudge

__all__ = ["S4ApolloCoherenceJudge"]

_SYSTEM_PROMPT = (
    "You are auditing whether an AI's confused-learner questions and "
    "clarification follow-ups, during one student-teaches-AI session, made "
    "sense given how the session was later graded. You see: the sequence of "
    "questions Apollo (the confused learner) asked, the clarification "
    "exchanges, the final set of ledger keys marked unresolved or "
    "misconception, and the clarification trace (question, answer, whether "
    "credit was granted). Judge ok=true only if BOTH hold: (1) Apollo's "
    "questions/follow-ups plausibly targeted concepts that ended up "
    "unresolved or misconceived — not concepts the student had already "
    "taught cleanly — and (2) every clarification exchange that granted "
    "credit is reflected in the ledger (not left unresolved), and every one "
    "that denied credit is not credited."
)


class S4ApolloCoherenceJudge(StageJudge):
    stage = "s4_apollo_coherence"
    system_prompt = _SYSTEM_PROMPT

    def build_items(self, raw: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """``raw`` = list of sampled sessions, each
        ``{attempt_id, apollo_questions, clarification_trace,
        unresolved_keys, misconception_keys}``. One item per session."""
        items: list[dict[str, Any]] = []
        for session in raw:
            items.append(
                {
                    "item_id": str(session.get("attempt_id")),
                    "apollo_questions": session.get("apollo_questions", []),
                    "clarification_trace": session.get("clarification_trace", []),
                    "unresolved_keys": session.get("unresolved_keys", []),
                    "misconception_keys": session.get("misconception_keys", []),
                }
            )
        return items

    def user_prompt(self, item: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "apollo_questions": item.get("apollo_questions"),
                "clarification_trace": item.get("clarification_trace"),
                "unresolved_keys": item.get("unresolved_keys"),
                "misconception_keys": item.get("misconception_keys"),
            },
            sort_keys=True,
        )
