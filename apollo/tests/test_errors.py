import pytest

from apollo.errors import (
    ApolloError,
    FilterRejectedError,
    MalformedEquationError,
    NoMatchingConceptError,
    ParserCouldNotExtractError,
    PoolExhaustedError,
    SessionFrozenError,
)


def test_all_errors_subclass_apollo_error():
    for exc in (
        FilterRejectedError,
        MalformedEquationError,
        NoMatchingConceptError,
        ParserCouldNotExtractError,
        PoolExhaustedError,
        SessionFrozenError,
    ):
        assert issubclass(exc, ApolloError)


def test_parser_error_carries_utterance():
    e = ParserCouldNotExtractError(utterance="pressure plus rho v squared")
    assert "pressure" in str(e)
    assert e.utterance == "pressure plus rho v squared"


def test_filter_error_carries_rejected_term():
    e = FilterRejectedError(rejected_term="continuity", draft="Use the continuity equation")
    assert e.rejected_term == "continuity"
    assert "continuity" in str(e)


def test_malformed_equation_error_carries_entry_id():
    e = MalformedEquationError(entry_id="bernoulli", symbolic="P1 + 1/2*rho*v^2", parse_error="unexpected token")
    assert e.entry_id == "bernoulli"
    assert "bernoulli" in str(e)


def test_no_matching_concept_error_is_raisable():
    with pytest.raises(NoMatchingConceptError):
        raise NoMatchingConceptError(transcript_summary="conversation about cooking")


def test_pool_exhausted_carries_cluster_and_difficulty():
    e = PoolExhaustedError(concept_cluster_id="fluid_mechanics", difficulty="hard")
    assert e.concept_cluster_id == "fluid_mechanics"
    assert e.difficulty == "hard"


def test_session_frozen_error_is_raisable():
    with pytest.raises(SessionFrozenError):
        raise SessionFrozenError(session_id="abc-123")
