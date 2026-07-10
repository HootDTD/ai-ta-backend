"""T1 smoke tests: clarification v2-ranker flag + params (spec §8.1).

Smoke tier mirroring apollo/resolver_v2/tests/test_config_types.py: happy
path + one malformed-env edge case per public function.
"""

import pytest

from apollo.clarification.v2_config import (
    CLARIFICATION_V2_RANKER_FLAG,
    ClarificationV2Params,
    clarification_v2_ranker_enabled,
    load_clarification_v2_params,
)

_PARAM_ENVS = (
    CLARIFICATION_V2_RANKER_FLAG,
    "APOLLO_CLARIFICATION_V2_MAX_QUESTIONS",
    "APOLLO_CLARIFICATION_V2_MAX_TOPICS_PER_QUESTION",
    "APOLLO_CLARIFICATION_V2_MAX_QUESTIONS_PER_ATTEMPT",
    "APOLLO_CLARIFICATION_V2_VOI_TARGET_CREDIT",
    "APOLLO_CLARIFICATION_V2_P_MISSING",
    "APOLLO_CLARIFICATION_V2_P_NEAR_RESOLVED",
    "APOLLO_CLARIFICATION_V2_P_GRAY_MIN",
    "APOLLO_CLARIFICATION_V2_P_GRAY_MAX",
    "APOLLO_CLARIFICATION_V2_P_EQUATION_FLOOR",
    "APOLLO_CLARIFICATION_V2_INCREMENTAL_DEADLINE_MS",
    "APOLLO_CLARIFICATION_V2_TRACE_TOP_N",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _PARAM_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


# --- flag ---------------------------------------------------------------


def test_flag_default_off():
    assert clarification_v2_ranker_enabled() is False


def test_flag_truthy_values(monkeypatch):
    for value in ("1", "true", "YES", " yes "):
        monkeypatch.setenv(CLARIFICATION_V2_RANKER_FLAG, value)
        assert clarification_v2_ranker_enabled() is True


def test_flag_non_truthy_values_stay_off(monkeypatch):
    for value in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv(CLARIFICATION_V2_RANKER_FLAG, value)
        assert clarification_v2_ranker_enabled() is False


def test_flag_fresh_read_per_call(monkeypatch):
    """No caching: toggling the env between calls must be honored
    immediately (mirrors resolver_v2's fresh-read invariant)."""
    assert clarification_v2_ranker_enabled() is False
    monkeypatch.setenv(CLARIFICATION_V2_RANKER_FLAG, "true")
    assert clarification_v2_ranker_enabled() is True
    monkeypatch.setenv(CLARIFICATION_V2_RANKER_FLAG, "false")
    assert clarification_v2_ranker_enabled() is False


# --- load_clarification_v2_params ----------------------------------------


def test_load_params_defaults():
    params = load_clarification_v2_params()
    assert params == ClarificationV2Params()
    assert params.max_questions == 3
    assert params.max_topics_per_question == 3
    assert params.max_questions_per_attempt == 12
    assert params.voi_target_credit == 1.0
    assert params.p_missing == 0.6
    assert params.p_near_resolved == 0.2
    assert params.p_gray_min == 0.3
    assert params.p_gray_max == 0.8
    assert params.p_equation_floor == 0.7
    assert params.incremental_deadline_ms == 1500
    assert params.trace_top_n == 10


def test_load_params_env_overrides(monkeypatch):
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_MAX_QUESTIONS", "5")
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_VOI_TARGET_CREDIT", "0.9")
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_INCREMENTAL_DEADLINE_MS", "2000")
    params = load_clarification_v2_params()
    assert params.max_questions == 5
    assert params.voi_target_credit == 0.9
    assert params.incremental_deadline_ms == 2000
    # untouched fields keep defaults
    assert params.max_topics_per_question == ClarificationV2Params().max_topics_per_question
    assert params.p_missing == ClarificationV2Params().p_missing


def test_load_params_malformed_falls_back(monkeypatch):
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_MAX_QUESTIONS", "not-an-int")
    monkeypatch.setenv("APOLLO_CLARIFICATION_V2_P_MISSING", "not-a-float")
    params = load_clarification_v2_params()
    assert params.max_questions == ClarificationV2Params().max_questions
    assert params.p_missing == ClarificationV2Params().p_missing


def test_params_frozen():
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        load_clarification_v2_params().max_questions = 99  # type: ignore[misc]
