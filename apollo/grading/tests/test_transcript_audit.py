"""WU-4B1 §6.2 — pure-unit tests for the batched transcript audit (mocked).

No live OpenAI call ever fires: every ``audit_missing`` call injects a
deterministic ``audit_fn``, and the ``main_chat_auditor`` seam patches
``apollo.grading.transcript_audit.main_chat`` (mirroring the adjudication test).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apollo.errors import TranscriptAuditUnavailableError
from apollo.grading.tests._builders import (
    found_audit_fn,
    notfound_audit_fn,
    raising_audit_fn,
)
from apollo.grading.transcript_audit import (
    AUDIT_TRANSCRIPT_CHAR_BUDGET,
    TRANSCRIPT_AUDIT_CONFIDENCE_CAP,
    TRANSCRIPT_AUDIT_METHOD,
    AliasCandidate,
    AuditResult,
    MissingEntity,
    audit_missing,
    main_chat_auditor,
)


def _entity(key: str) -> MissingEntity:
    return MissingEntity(canonical_key=key, display_name=key)


# --- value types / caps -----------------------------------------------------


def test_constants():
    assert TRANSCRIPT_AUDIT_CONFIDENCE_CAP == 0.75
    assert TRANSCRIPT_AUDIT_METHOD == "transcript_audit"


def test_alias_candidate_default_confidence_is_cap():
    cand = AliasCandidate(canonical_key="eq.x", span="because v rises")
    assert cand.confidence == TRANSCRIPT_AUDIT_CONFIDENCE_CAP
    assert cand.canonical_key == "eq.x"
    assert cand.span == "because v rises"


def test_audit_result_shape():
    res = AuditResult(upgraded_keys=frozenset({"a"}), spans_by_key={"a": "s"}, alias_candidates=())
    assert res.upgraded_keys == frozenset({"a"})
    assert res.spans_by_key == {"a": "s"}
    assert res.alias_candidates == ()


# --- audit_missing paths ----------------------------------------------------


def test_empty_missing_entities_no_call():
    spy = found_audit_fn({})
    result = audit_missing((), "anything", audit_fn=spy)
    assert result.upgraded_keys == frozenset()
    assert result.spans_by_key == {}
    assert result.alias_candidates == ()
    assert spy.requests == []  # type: ignore[attr-defined]


def test_span_found_upgrades_key_and_emits_alias():
    fn = found_audit_fn({"eq.continuity": "the pipe narrows so v increases"})
    result = audit_missing((_entity("eq.continuity"),), "transcript", audit_fn=fn)
    assert "eq.continuity" in result.upgraded_keys
    assert result.spans_by_key["eq.continuity"] == "the pipe narrows so v increases"
    assert len(result.alias_candidates) == 1
    cand = result.alias_candidates[0]
    assert cand.canonical_key == "eq.continuity"
    assert cand.span == "the pipe narrows so v increases"
    assert cand.confidence == 0.75


def test_alias_candidate_confidence_is_audit_cap_not_alias_tier():
    fn = found_audit_fn({"eq.x": "span"})
    result = audit_missing((_entity("eq.x"),), "t", audit_fn=fn)
    assert result.alias_candidates[0].confidence == 0.75
    assert result.alias_candidates[0].confidence != 0.92


def test_span_none_leaves_key_missing():
    fn = notfound_audit_fn()
    result = audit_missing((_entity("eq.x"),), "t", audit_fn=fn)
    assert "eq.x" not in result.upgraded_keys
    assert result.alias_candidates == ()


def test_unasked_returned_key_ignored():
    def fn(request):
        return {"eq.x": "real", "eq.NOTASKED": "leak"}

    result = audit_missing((_entity("eq.x"),), "t", audit_fn=fn)
    assert "eq.NOTASKED" not in result.upgraded_keys
    assert "eq.NOTASKED" not in result.spans_by_key
    assert result.upgraded_keys == frozenset({"eq.x"})


def test_multiple_entities_partial_found():
    fn = found_audit_fn({"a": "span-a", "c": "span-c"})  # b missing
    entities = (_entity("a"), _entity("b"), _entity("c"))
    result = audit_missing(entities, "t", audit_fn=fn)
    assert result.upgraded_keys == frozenset({"a", "c"})
    # deterministic ordering: alias candidates sorted by canonical_key.
    assert [c.canonical_key for c in result.alias_candidates] == ["a", "c"]


def test_injected_audit_fn_raise_propagates():
    fn = raising_audit_fn()
    with pytest.raises(TranscriptAuditUnavailableError):
        audit_missing((_entity("a"),), "t", audit_fn=fn)


# --- main_chat_auditor seam (mirrors the adjudicator test) ------------------


def test_main_chat_auditor_one_call():
    payload = json.dumps({"spans": {"eq.x": "a quote"}})
    entities = (_entity("eq.x"),)
    with patch("apollo.grading.transcript_audit.main_chat", return_value=payload) as mock_chat:
        result = audit_missing(entities, "short transcript", audit_fn=main_chat_auditor)
    assert mock_chat.call_count == 1
    assert result.spans_by_key["eq.x"] == "a quote"


def test_main_chat_auditor_transient_failure_named():
    def _boom(**kw):
        raise RuntimeError("openai 503")

    with patch("apollo.grading.transcript_audit.main_chat", side_effect=_boom):
        with pytest.raises(TranscriptAuditUnavailableError) as exc:
            audit_missing((_entity("a"),), "t", audit_fn=main_chat_auditor)
    assert exc.value.stage == "transcript_audit"


def test_main_chat_auditor_malformed_json_named():
    with patch("apollo.grading.transcript_audit.main_chat", return_value="not json"):
        with pytest.raises(TranscriptAuditUnavailableError):
            audit_missing((_entity("a"),), "t", audit_fn=main_chat_auditor)


def test_main_chat_auditor_reraises_named_verbatim():
    inner = TranscriptAuditUnavailableError(last_error="already named")
    with patch("apollo.grading.transcript_audit.main_chat", side_effect=inner):
        with pytest.raises(TranscriptAuditUnavailableError) as exc:
            audit_missing((_entity("a"),), "t", audit_fn=main_chat_auditor)
    assert exc.value is inner


def test_main_chat_auditor_null_span_parsed_as_not_found():
    payload = json.dumps({"spans": {"eq.x": None}})
    with patch("apollo.grading.transcript_audit.main_chat", return_value=payload):
        result = audit_missing((_entity("eq.x"),), "t", audit_fn=main_chat_auditor)
    assert result.upgraded_keys == frozenset()


# --- chunking ---------------------------------------------------------------


def test_long_transcript_chunked_span_in_later_chunk():
    long_transcript = "x" * (AUDIT_TRANSCRIPT_CHAR_BUDGET * 2 + 10)  # 3 chunks
    calls = {"n": 0}

    def fn(request):
        calls["n"] += 1
        # entities re-asked every chunk; the span is found only on the 2nd chunk.
        if calls["n"] == 2:
            return {e.canonical_key: "found-in-chunk-2" for e in request.entities}
        return {e.canonical_key: None for e in request.entities}

    result = audit_missing((_entity("eq.x"),), long_transcript, audit_fn=fn)
    assert calls["n"] == 3  # 3 chunks => 3 asks (entities re-asked per chunk)
    assert "eq.x" in result.upgraded_keys
    assert result.spans_by_key["eq.x"] == "found-in-chunk-2"


def test_short_transcript_is_single_chunk():
    fn = found_audit_fn({"eq.x": "span"})
    audit_missing((_entity("eq.x"),), "short", audit_fn=fn)
    assert len(fn.requests) == 1  # type: ignore[attr-defined]


def test_span_found_in_first_chunk_wins_over_later():
    long_transcript = "y" * (AUDIT_TRANSCRIPT_CHAR_BUDGET + 5)  # 2 chunks
    calls = {"n": 0}

    def fn(request):
        calls["n"] += 1
        return {e.canonical_key: f"chunk-{calls['n']}" for e in request.entities}

    result = audit_missing((_entity("eq.x"),), long_transcript, audit_fn=fn)
    # FIRST span found for a key is kept (spans deduped; chunk-1 wins).
    assert result.spans_by_key["eq.x"] == "chunk-1"
