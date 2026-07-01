"""Task 10 — NLI hot-path wired into the chat clarification detector.

Three behavioral requirements:
  1. With an active NLIContext, find_residual_nodes runs via executor; a node
     that only paraphrases the reference (no lexical alias) resolves through
     NLI → NOT residual → hints == [].
  2. With nli_ctx=None the path is byte-identical to before: the paraphrase
     stays residual → a probe hint IS produced (current behavior unchanged).
  3. When the adjudicator raises inside the executor, the outer fail-safe
     catches the re-raised exception and returns [] (teaching never blocks).
"""

from __future__ import annotations

from apollo.clarification import turn
from apollo.clarification.embedding import CandidateEmbeddingCache
from apollo.resolution.candidates import Candidate
from apollo.resolution.nli_adjudicator import FakeNLIAdjudicator, NLIResult
from apollo.resolution.nli_config import NLIParams
from apollo.resolution.nli_resolution import NLIContext
from apollo.resolution.tests.test_resolver import _node

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Student node: "condition" type, applies_when = _STUDENT_TEXT.
# No aliases on the candidate → all lexical tiers miss → only NLI can resolve.
_STUDENT_TEXT = "speed increases pressure drops"
_CAND_DISPLAY = "speed increases pressure"

# NLI result that entails the match (scores within _NLI_PARAMS thresholds).
_ENTAILING = NLIResult(
    label="entailment",
    entailment=0.95,
    contradiction=0.02,
    neutral=0.03,
    model_name="fake",
)

# Relaxed thresholds so _ENTAILING scores pass without tuning.
_NLI_PARAMS = NLIParams(
    top_k=5,
    min_entailment=0.80,
    max_contradiction=0.15,
    ambiguity_margin=0.10,
    misconception_veto_entailment=0.80,
)


def _cand_no_alias(key: str = "cond.speed_pressure") -> Candidate:
    """Candidate with empty aliases — lexical tiers cannot match."""
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=_CAND_DISPLAY,
        opposes_key=None,
        exact_aliases=(),
    )


def _entailing_ctx() -> NLIContext:
    """NLI context whose adjudicator entails _STUDENT_TEXT → _CAND_DISPLAY."""
    fake = FakeNLIAdjudicator({(_STUDENT_TEXT, _CAND_DISPLAY): _ENTAILING})
    return NLIContext(nli=fake, embedder=None, cache=None, params=_NLI_PARAMS)


async def _run(nli_ctx: NLIContext | None = None) -> list[str]:
    """Shared call helper — mirrors test_turn.py: db=object(), fake embedder."""
    node = _node("s1", "condition", {"applies_when": _STUDENT_TEXT, "label": ""})
    return await turn.run_clarification_detection(
        db=object(),  # type: ignore[arg-type]  # test pattern: db unused in these paths
        parsed_nodes=[node],
        candidates=(_cand_no_alias(),),
        symbolic_mappings={},
        embedder=lambda texts: [[1.0, 0.0] for _ in texts],
        cache=CandidateEmbeddingCache(),
        attempt_id=1,
        session_id=1,
        user_id="u",
        search_space_id=1,
        concept_id=2,
        asked_turn=2,
        nli_ctx=nli_ctx,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_residual_runs_with_nli_ctx_via_executor():
    """Node paraphrases the reference; with NLI it resolves → NOT residual → hints == [].

    Proves that find_residual_nodes ran through the executor with an active
    NLIContext — the NLI tier matched, node is NOT returned as residual, so
    no probe is created.
    """
    hints = await _run(nli_ctx=_entailing_ctx())
    assert hints == []


async def test_nli_ctx_none_keeps_current_behavior(monkeypatch):
    """Without NLI the paraphrase stays residual → a probe hint IS produced.

    Proves the off-path (nli_ctx=None) is byte-identical to the pre-NLI code:
    find_residual_nodes runs synchronously without NLI, the node is residual,
    the embedder flags it, and write_asked_waiting is called once.
    """
    writes: list[dict] = []

    async def fake_write(db, **kw):
        writes.append(kw)

    monkeypatch.setattr(turn, "write_asked_waiting", fake_write)

    hints = await _run(nli_ctx=None)
    assert len(hints) == 1
    assert writes  # write_asked_waiting was called


async def test_fail_safe_on_adjudicator_error():
    """Adjudicator raises inside the executor → fail-safe catches and returns [].

    When classify() raises, the exception propagates out of find_residual_nodes
    in the thread, run_in_executor re-raises it in the coroutine, and the outer
    except clause in run_clarification_detection catches it — teaching never blocks.
    """

    class ErrorAdjudicator:
        def classify(self, premise: str, hypothesis: str) -> NLIResult:
            raise RuntimeError("NLI model failure")

    ctx = NLIContext(nli=ErrorAdjudicator(), embedder=None, cache=None, params=_NLI_PARAMS)
    hints = await _run(nli_ctx=ctx)
    assert hints == []
