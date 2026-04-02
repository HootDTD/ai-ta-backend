"""Tests for graduated relevance classification in main_ai."""
from __future__ import annotations

import json
import types

import ai.main_ai as mai


def _make_fake_client(response_json: dict | str | None = None):
    """Build a fake OpenAI client whose chat.completions.create returns *response_json*."""
    if response_json is None:
        response_json = {}
    content = json.dumps(response_json) if isinstance(response_json, dict) else response_json

    class _Completions:
        def create(self, *args, **kwargs):
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=content)
                )]
            )

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=_Completions()))


# ---------------------------------------------------------------------------
# check_question_relevance — graduated format
# ---------------------------------------------------------------------------

def test_full_relevance(monkeypatch):
    """Fully on-topic question should return relevance='full'."""
    fake = {
        "relevance": "full",
        "on_topic_portion": "What is Bernoulli's equation?",
        "off_topic_portion": "",
        "reason": "Entirely about fluid mechanics",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    result = mai.check_question_relevance("What is Bernoulli's equation?", "Fluid Mechanics")
    assert result["relevance"] == "full"
    assert result["on_topic_portion"] == "What is Bernoulli's equation?"
    assert result["off_topic_portion"] == ""
    mai._cached_client = None


def test_partial_relevance(monkeypatch):
    """Mixed question should return relevance='partial' with extracted portions."""
    fake = {
        "relevance": "partial",
        "on_topic_portion": "What is Bernoulli's equation",
        "off_topic_portion": "how does it relate to mcdonalds",
        "reason": "Bernoulli's is course material; McDonald's is not",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    result = mai.check_question_relevance(
        "What is Bernoulli's and how does it relate to mcdonalds",
        "Fluid Mechanics",
    )
    assert result["relevance"] == "partial"
    assert "Bernoulli" in result["on_topic_portion"]
    assert "mcdonalds" in result["off_topic_portion"]
    mai._cached_client = None


def test_none_relevance(monkeypatch):
    """Completely off-topic question should return relevance='none'."""
    fake = {
        "relevance": "none",
        "on_topic_portion": "",
        "off_topic_portion": "What is the best pizza topping?",
        "reason": "Nothing to do with fluid mechanics",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    result = mai.check_question_relevance("What is the best pizza topping?", "Fluid Mechanics")
    assert result["relevance"] == "none"
    assert result["on_topic_portion"] == ""
    mai._cached_client = None


# ---------------------------------------------------------------------------
# Backward compatibility — old binary format
# ---------------------------------------------------------------------------

def test_legacy_bool_true(monkeypatch):
    """Old-format {relevant: true} should map to relevance='full'."""
    fake = {"relevant": True, "reason": "About the course"}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    result = mai.check_question_relevance("What is drag?", "Aerodynamics")
    assert result["relevance"] == "full"
    mai._cached_client = None


def test_legacy_bool_false(monkeypatch):
    """Old-format {relevant: false} should map to relevance='none'."""
    fake = {"relevant": False, "reason": "Off topic"}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    result = mai.check_question_relevance("Best pizza?", "Aerodynamics")
    assert result["relevance"] == "none"
    mai._cached_client = None


def test_legacy_string_yes(monkeypatch):
    """Old-format {relevant: 'yes'} should map to relevance='full'."""
    fake = {"relevant": "yes", "reason": "On topic"}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    result = mai.check_question_relevance("What is lift?", "Aerodynamics")
    assert result["relevance"] == "full"
    mai._cached_client = None


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------

def test_fail_open_on_exception(monkeypatch):
    """On API failure, should fail open with relevance='full'."""

    class _FailingCompletions:
        def create(self, *args, **kwargs):
            raise ConnectionError("API down")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_FailingCompletions())
    )

    result = mai.check_question_relevance("What is drag?", "Aerodynamics")
    assert result["relevance"] == "full"
    mai._cached_client = None


def test_empty_question():
    """Empty question should return relevance='none'."""
    result = mai.check_question_relevance("", "Aerodynamics")
    assert result["relevance"] == "none"


# ---------------------------------------------------------------------------
# is_question_subject_relevant — backward-compatible wrapper
# ---------------------------------------------------------------------------

def test_wrapper_true_for_full(monkeypatch):
    """Wrapper should return True for 'full' relevance."""
    fake = {"relevance": "full", "on_topic_portion": "q", "off_topic_portion": "", "reason": ""}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    assert mai.is_question_subject_relevant("What is lift?", "Aero") is True
    mai._cached_client = None


def test_wrapper_true_for_partial(monkeypatch):
    """Wrapper should return True for 'partial' — the on-topic part should be answered."""
    fake = {
        "relevance": "partial",
        "on_topic_portion": "Bernoulli",
        "off_topic_portion": "mcdonalds",
        "reason": "",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    assert mai.is_question_subject_relevant("Bernoulli and mcdonalds", "Fluids") is True
    mai._cached_client = None


def test_wrapper_false_for_none(monkeypatch):
    """Wrapper should return False for 'none' relevance."""
    fake = {"relevance": "none", "on_topic_portion": "", "off_topic_portion": "pizza", "reason": ""}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake)

    assert mai.is_question_subject_relevant("Best pizza?", "Aero") is False
    mai._cached_client = None
