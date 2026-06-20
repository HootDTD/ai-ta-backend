"""WU-3C2 Step 8 — pure-unit tests for the single LLM adjudication call (mocked).

No live OpenAI call ever fires: ``apollo.resolution.adjudication.main_chat`` is
patched. Pin (§5 step 5):
- exactly ONE main_chat call for N remaining ambiguous nodes (call_count == 1);
- "return empty when unsure" -> those nodes stay unresolved (no key);
- a hallucinated key (not in the candidate set) -> ResolutionInvalidOutputError;
- a transient main_chat failure -> ResolutionUnavailableError(llm_adjudication);
- an LLM-resolved node is reported with method 'llm'.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apollo.errors import ResolutionInvalidOutputError, ResolutionUnavailableError
from apollo.ontology.nodes import build_node
from apollo.resolution.adjudication import adjudicate, main_chat_adjudicator
from apollo.resolution.candidates import Candidate


def _cand(key, node_type="condition"):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type=node_type,
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=key,
        opposes_key=None,
    )


def _node(node_id, node_type="condition"):
    content = {
        "condition": {"applies_when": "something vague", "label": ""},
    }[node_type]
    return build_node(
        node_type=node_type, node_id=node_id, attempt_id=1, source="parser", content=content
    )


_CANDS = (_cand("cond.a"), _cand("cond.b"))


def test_one_llm_call_max_for_all_remaining():
    remaining = [_node("s1"), _node("s2"), _node("s3")]
    payload = json.dumps({"resolutions": {"s1": "cond.a"}})
    with patch("apollo.resolution.adjudication.main_chat", return_value=payload) as mock_chat:
        adjudicate(remaining, _CANDS, adjudicator=main_chat_adjudicator)
    assert mock_chat.call_count == 1


def test_llm_return_empty_keeps_unresolved():
    remaining = [_node("s1"), _node("s2")]
    payload = json.dumps({"resolutions": {}})
    with patch("apollo.resolution.adjudication.main_chat", return_value=payload):
        result = adjudicate(remaining, _CANDS, adjudicator=main_chat_adjudicator)
    assert result == {}  # nothing resolved


def test_llm_hallucinated_key_raises_invalid_output():
    remaining = [_node("s1")]
    payload = json.dumps({"resolutions": {"s1": "cond.NONEXISTENT"}})
    with patch("apollo.resolution.adjudication.main_chat", return_value=payload):
        with pytest.raises(ResolutionInvalidOutputError) as exc:
            adjudicate(remaining, _CANDS, adjudicator=main_chat_adjudicator)
    assert exc.value.returned_key == "cond.NONEXISTENT"
    assert "cond.a" in exc.value.allowed_keys


def test_llm_transient_failure_raises_unavailable():
    remaining = [_node("s1")]

    def _boom(**kw):
        raise RuntimeError("openai 503")

    with patch("apollo.resolution.adjudication.main_chat", side_effect=_boom):
        with pytest.raises(ResolutionUnavailableError) as exc:
            adjudicate(remaining, _CANDS, adjudicator=main_chat_adjudicator)
    assert exc.value.stage == "llm_adjudication"


def test_llm_resolves_node_to_candidate_key():
    remaining = [_node("s1")]
    payload = json.dumps({"resolutions": {"s1": "cond.b"}})
    with patch("apollo.resolution.adjudication.main_chat", return_value=payload):
        result = adjudicate(remaining, _CANDS, adjudicator=main_chat_adjudicator)
    assert result["s1"].canonical_key == "cond.b"


def test_empty_remaining_makes_no_call():
    with patch("apollo.resolution.adjudication.main_chat") as mock_chat:
        result = adjudicate([], _CANDS, adjudicator=main_chat_adjudicator)
    assert result == {}
    assert mock_chat.call_count == 0


def test_adjudicator_reraises_named_error_unwrapped():
    """If main_chat itself raises a ResolutionUnavailableError it is re-raised
    verbatim, not double-wrapped (the defensive re-raise branch)."""
    remaining = [_node("s1")]
    inner = ResolutionUnavailableError(stage="llm_adjudication", last_error="already named")
    with patch("apollo.resolution.adjudication.main_chat", side_effect=inner):
        with pytest.raises(ResolutionUnavailableError) as exc:
            adjudicate(remaining, _CANDS, adjudicator=main_chat_adjudicator)
    assert exc.value is inner
