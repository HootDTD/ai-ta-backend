"""The patched-seam methodology behind every deterministic gate in this suite.

Live LLMs are non-deterministic at temperature 0 (7-18% of letters and 16.7% of
node credits move between identical runs, spec §4), so **byte-identity can only
ever be asserted against a PATCHED producer**. Every seam that could reach a
model is pinned here and the ONLY free variable a gate changes is
``APOLLO_WRONGNESS_LEVEL``:

* **per-turn producer** — not called at all on the Done path; the ledger is
  seeded directly (``wf.contested_ledger``). The producer's own level-0 identity
  is pinned by W1-A's sha256 schema/prompt tests and re-asserted structurally in
  ``test_gate_l1_inertness.py``.
* **at-Done adjudicator** — ``done.compute_transcript_coverage_with_spans`` is
  replaced with a recorded verdict (the pattern
  ``campaign/transcript_replay.py`` uses via
  ``patch("...transcript_coverage._call_adjudication")``); the offline
  fixture-playback gates use that lower seam through
  ``campaign.turn_replay.replay_recorded``.
* **narrator** — the REAL ``generate_diagnostic`` runs (so the real
  ``build_topic_narrative_prompt`` renders the real level-3 misconception line),
  with ``apollo.overseer.diagnostic.bounded_client`` replaced by a fake that
  (a) returns a FIXED completion, so the narrative text is deterministic, and
  (b) records a sha256 of the ``(system, user)`` prompt pair, so a gate can ask
  the sharper question: *did the narrator's INPUT move between levels?*
  (The seam is ``apollo.agent._llm.bounded_client``, bound into
  ``apollo.overseer.diagnostic`` at import; patching the origin module would not
  rebind the already-imported name, so the patch lands on the narrator module.)
* **network** — ``tests/gates_p32/conftest.py`` refuses any non-loopback
  ``socket.connect`` for the whole suite, so a seam that escaped the list above
  fails loudly instead of passing slowly.

``score_fingerprint`` is the R6 sharpening of §4's "whole-blob byte-identical"
shorthand: level 3's entire purpose is populating
``topics[].misconceptions``, so literal whole-blob equality is unsatisfiable
there. Exactly one additive path is permitted, enumerated in
``PERMITTED_ADDITIVE_PATHS``, and every other field — ``llm_rubric`` verbatim
included — is compared unprojected.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from apollo.handlers.done import handle_done
from apollo.handlers.tests import _wrongness_fixtures as wf
from apollo.handlers.tests._done_fixtures import _old_path_patches
from apollo.overseer import diagnostic as diagnostic_module
from apollo.overseer.diagnostic import generate_diagnostic as real_generate_diagnostic

DEFAULT = wf.DEFAULT

#: The ONLY field level 3 is allowed to add to the persisted grading blob.
#: Written from the ARTIFACT root (``build_llm_artifact``'s payload), while
#: :func:`score_fingerprint` is handed ``payload["scores"]`` — i.e.
#: ``GradingRun.score_details``, the column §4's G-L1/G-L3 rows name.
PERMITTED_ADDITIVE_PATHS: tuple[str, ...] = ("scores.topic_score.topics[].misconceptions",)

#: What the fake narrator returns. Deliberately NOT valid topic-feedback JSON:
#: ``_parse_topic_feedback`` then soft-fails to ``_legacy_narrative``, which is a
#: pure function of (this string, rubric, coverage, topic ledger keys) — all of
#: which a gate already compares — so the narrative text is deterministic and any
#: movement in it is a real signal rather than a JSON-shape artifact.
NARRATOR_COMPLETION = "Fixed narrator output for the P3.2 gate suite."


# --------------------------------------------------------------------------- #
# The inertness projection                                                     #
# --------------------------------------------------------------------------- #


def strip_permitted_additive(score_details: Mapping[str, Any]) -> dict[str, Any]:
    """A deep copy of ``score_details`` with ``PERMITTED_ADDITIVE_PATHS`` removed."""
    stripped: dict[str, Any] = copy.deepcopy(dict(score_details))
    topic_score = stripped.get("topic_score")
    if isinstance(topic_score, dict):
        for topic in topic_score.get("topics") or ():
            if isinstance(topic, dict):
                topic.pop("misconceptions", None)
    return stripped


def score_fingerprint(score_details: Mapping[str, Any]) -> str:
    """sha256 over ``score_details`` minus the one permitted additive path."""
    payload = json.dumps(strip_permitted_additive(score_details), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def blob(value: Any) -> str:
    """Canonical JSON for a literal whole-blob equality assertion."""
    return json.dumps(value, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# The narrator fake                                                            #
# --------------------------------------------------------------------------- #


class _NarratorRecorder:
    """A ``bounded_client()`` stand-in that hashes what it was asked."""

    def __init__(self) -> None:
        self.digests: list[str] = []
        self.chat = MagicMock()
        self.chat.completions = MagicMock()
        self.chat.completions.create = self._create

    def _create(self, **kwargs: Any) -> Any:
        messages = kwargs.get("messages") or []
        material = json.dumps(
            [(m.get("role"), m.get("content")) for m in messages], sort_keys=True, default=str
        )
        self.digests.append(hashlib.sha256(material.encode("utf-8")).hexdigest())
        message = MagicMock()
        message.content = NARRATOR_COMPLETION
        choice = MagicMock()
        choice.message = message
        response = MagicMock()
        response.choices = [choice]
        return response


# --------------------------------------------------------------------------- #
# The level-N Done                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GateRun:
    """One ``handle_done`` at one rung, with everything a §4 gate compares."""

    level: int
    #: The payload the STUDENT was served (`handle_done`'s return).
    student_response: dict[str, Any]
    #: ``GradingRun.score_details`` — the persisted ``payload["scores"]`` blob.
    score_details: dict[str, Any]
    #: ``attempt.diagnostic_report``: narrative + rubric + coverage + served_overall.
    diagnostic_report: dict[str, Any]
    #: The canonical artifact payload the scorecard is templated from.
    artifact: dict[str, Any]
    #: ``grader_payload -> 'misconceptions'``: the INTERNAL array (levels >= 1).
    shadow_misconceptions: list[dict[str, Any]] | None
    #: The candidate map handed to the at-Done corroborator (S5), or ``None``.
    wrongness_candidates: dict[str, str] | None
    xp_delta: int
    #: sha256 of the narrator's (system, user) prompt pair — empty when the
    #: narrator never ran.
    narrator_digests: tuple[str, ...]

    @property
    def topic_score(self) -> dict[str, Any]:
        return dict(self.score_details.get("topic_score") or {})

    @property
    def served_misconceptions(self) -> list[dict[str, Any]]:
        return list(self.artifact.get("misconceptions") or [])

    @property
    def topic_misconceptions(self) -> list[dict[str, Any]]:
        return [
            misconception
            for topic in self.topic_score.get("topics") or ()
            for misconception in topic.get("misconceptions") or ()
        ]


async def run_gate_done(
    monkeypatch: Any,
    *,
    level: int,
    ledger: Any = DEFAULT,
    wrongness_map: Any = DEFAULT,
    prior_findings: Sequence[Mapping[str, Any]] = (),
    concept_allowlist: str | None = None,
) -> GateRun:
    """Drive ONE ``handle_done`` at ``APOLLO_WRONGNESS_LEVEL=level``.

    Identical seeded state at every rung — the same ledger, the same recorded
    adjudication, the same reference graph — so a difference in the output is a
    difference the LADDER made and nothing else.
    """
    monkeypatch.setenv("APOLLO_WRONGNESS_LEVEL", str(level))
    if concept_allowlist is None:
        monkeypatch.delenv("INTERACTION_CONCEPTS", raising=False)
    else:
        monkeypatch.setenv("INTERACTION_CONCEPTS", concept_allowlist)
    if ledger is DEFAULT:
        ledger = wf.contested_ledger()
    if wrongness_map is DEFAULT:
        wrongness_map = wf.second_reader()

    db, sess, attempt, patches = _old_path_patches()
    graph = wf.reference_graph()
    captured: dict[str, Any] = {"artifact": {}, "shadow": None}

    async def _find_problem_with_graph(_db: Any, _cid: Any, _code: Any, *, course_id: int) -> Any:
        problem = MagicMock()
        problem.id = "p_code"
        problem.database_id = 42
        problem.concept_id = wf.CONCEPT_SLUG
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    async def _write_artifacts(_db: Any, **kwargs: Any) -> dict[str, Any]:
        """The REAL builder minus the INSERT, so the scorecard and the persisted
        ``score_details`` are the genuine artifact rather than a hand-written
        dict that could agree with a fiction."""
        from apollo.grading.artifact_build import build_llm_artifact

        payload = build_llm_artifact(
            coverage=kwargs["coverage"],
            rubric=kwargs["rubric"],
            latency_ms=kwargs["latency_ms"],
            clarification_trace=[],
            topic_score=kwargs["topic_score"],
        )
        captured["artifact"] = payload
        captured["shadow"] = kwargs.get("shadow_misconceptions")
        return payload

    # Every seam the ladder could move, patched to a recorded constant. Dropping
    # `generate_diagnostic` puts the REAL narrator back on the path (its LLM call
    # is intercepted below), so the level-3 misconception line genuinely renders.
    drop = {
        "_find_problem",
        "compute_transcript_coverage_with_spans",
        "compute_topic_score",
        "_question_ledger",
        "compute_rubric",
        "write_artifacts",
        "generate_diagnostic",
    }
    narrator = _NarratorRecorder()
    coverage_call = AsyncMock(return_value=(wf.coverage_dict(wrongness_map), {}))
    patches = [p for p in patches if getattr(p, "attribute", None) not in drop]
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch("apollo.handlers.done._question_ledger", new=AsyncMock(return_value=ledger)),
        patch("apollo.handlers.done.compute_transcript_coverage_with_spans", new=coverage_call),
        patch(
            "apollo.handlers.done.prior_wrongness_findings",
            new=AsyncMock(return_value=tuple(prior_findings)),
        ),
        patch("apollo.handlers.done.generate_diagnostic", new=real_generate_diagnostic),
        patch.object(diagnostic_module, "bounded_client", lambda: narrator),
        patch("apollo.handlers.done.write_artifacts", new=AsyncMock(side_effect=_write_artifacts)),
        patch("apollo.handlers.done._project_mastery", new=AsyncMock()),
    ]

    started: dict[str, Any] = {}
    for p in patches:
        started[getattr(p, "attribute", repr(p))] = p.start()
    try:
        student_response = await handle_done(db=db, neo=MagicMock(), session_id=int(sess.id))
    finally:
        for p in reversed(patches):
            p.stop()

    # The adjudicator is awaited exactly once on every non-refused Done, so a
    # missing `await_args` is a harness defect (a seam that stopped being
    # reached), not a case to degrade past.
    assert coverage_call.await_args is not None, "the adjudicator seam was never awaited"
    return GateRun(
        level=level,
        student_response=student_response,
        score_details=dict(captured["artifact"].get("scores") or {}),
        diagnostic_report=dict(attempt.diagnostic_report or {}),
        artifact=dict(captured["artifact"]),
        shadow_misconceptions=captured["shadow"],
        wrongness_candidates=coverage_call.await_args.kwargs.get("wrongness_candidates"),
        xp_delta=int(started["apply_xp"].await_args.kwargs["xp_delta"]),
        narrator_digests=tuple(narrator.digests),
    )
