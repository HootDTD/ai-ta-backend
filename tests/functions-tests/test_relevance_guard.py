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
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
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


# ---------------------------------------------------------------------------
# Topic-anchored guard — current_topic threads into prompt and payload
# ---------------------------------------------------------------------------


def _make_capturing_client(response_json: dict):
    """Fake OpenAI client that records the kwargs of the last create() call."""
    content = json.dumps(response_json)

    class _Completions:
        def __init__(self):
            self.kwargs = None

        def create(self, *args, **kwargs):
            self.kwargs = kwargs
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
            )

    completions = _Completions()
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    return client, completions


def test_prompt_without_topic_has_no_topic_line():
    """Without a current_topic the prompt must not mention one."""
    from ai.prompts.relevance_guard import relevance_guard_prompt

    prompt = relevance_guard_prompt("Management Information Systems")
    assert "currently studying" not in prompt


def test_prompt_with_topic_anchors_the_current_topic():
    """With a current_topic the prompt names it and marks it on-topic."""
    from ai.prompts.relevance_guard import relevance_guard_prompt

    prompt = relevance_guard_prompt(
        "Management Information Systems",
        current_topic="ethics: passive observation and surveillance",
    )
    assert "ethics: passive observation and surveillance" in prompt
    assert "currently studying" in prompt


def test_prompt_with_topic_anchors_rules_on_course_scope():
    """With a current_topic the classification RULES themselves must judge
    against the combined course scope, not the subject title alone — a
    subject-anchored "none" rule overrides the topic line whenever the module
    sounds like a different discipline (2026-07-31 ethics-in-MIS aside bug)."""
    from ai.prompts.relevance_guard import relevance_guard_prompt

    subject = "Management Information Systems"
    prompt = relevance_guard_prompt(
        subject, current_topic="ethics — current problem: objective vs subjective claims"
    )

    scope = "the course scope (its subject or the current module)"
    assert f'"full": The entire question is within {scope}.' in prompt
    assert f"different discipline than {subject}" in prompt
    assert "AND unrelated to the current module" in prompt
    # No rule may anchor on the bare subject title anymore.
    assert f"is about {subject}" not in prompt
    assert f"nothing to do with {subject}" not in prompt


def test_prompt_without_topic_keeps_subject_scope():
    """Without a current_topic the rules anchor on the subject's course scope."""
    from ai.prompts.relevance_guard import relevance_guard_prompt

    prompt = relevance_guard_prompt("Aerodynamics")
    assert "the Aerodynamics course scope" in prompt
    assert "AND unrelated to the current module" not in prompt


def test_current_topic_reaches_system_prompt_and_payload(monkeypatch):
    """current_topic must land in both the system prompt and the JSON payload."""
    fake = {"relevance": "full", "on_topic_portion": "q", "off_topic_portion": "", "reason": ""}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client, completions = _make_capturing_client(fake)
    mai._cached_client = client

    mai.check_question_relevance(
        "What are some examples of surveillance tools?",
        "Management Information Systems",
        current_topic="ethics: passive observation",
    )
    mai._cached_client = None

    system = completions.kwargs["messages"][0]["content"]
    payload = json.loads(completions.kwargs["messages"][1]["content"])
    assert "ethics: passive observation" in system
    assert payload["current_topic"] == "ethics: passive observation"


def test_payload_omits_current_topic_when_absent(monkeypatch):
    """No current_topic → no payload key and no topic line in the prompt."""
    fake = {"relevance": "full", "on_topic_portion": "q", "off_topic_portion": "", "reason": ""}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client, completions = _make_capturing_client(fake)
    mai._cached_client = client

    mai.check_question_relevance("What is drag?", "Aerodynamics")
    mai._cached_client = None

    system = completions.kwargs["messages"][0]["content"]
    payload = json.loads(completions.kwargs["messages"][1]["content"])
    assert "current_topic" not in payload
    assert "currently studying" not in system


# ---------------------------------------------------------------------------
# Orchestrator guard — per-request course subject reaches the classifier
# ---------------------------------------------------------------------------


def test_orchestrator_guard_passes_cfg_subject(monkeypatch):
    """Hoot chat's guard must classify against cfg.subject_name, not the global."""
    import ai.orchestrator as orch_mod
    from ai.orchestrator import Orchestrator
    from config.settings import RequestConfig

    captured = {}

    def _fake_guard(question, subject=None, current_topic=None):
        captured["subject"] = subject
        return {
            "relevance": "full",
            "on_topic_portion": question,
            "off_topic_portion": "",
            "reason": "",
        }

    monkeypatch.setattr(orch_mod, "check_question_relevance", _fake_guard)

    orch = Orchestrator(cfg=RequestConfig(subject_name="Management Information Systems"))
    result = orch._check_question_relevance("What are some examples of surveillance tools?")

    assert result["relevance"] == "full"
    assert captured["subject"] == "Management Information Systems"


def test_orchestrator_guard_falls_back_to_global_subject(monkeypatch):
    """Without a cfg the guard still resolves a subject via get_subject_name()."""
    import ai.orchestrator as orch_mod
    from ai.orchestrator import Orchestrator

    captured = {}

    def _fake_guard(question, subject=None, current_topic=None):
        captured["subject"] = subject
        return {
            "relevance": "full",
            "on_topic_portion": question,
            "off_topic_portion": "",
            "reason": "",
        }

    monkeypatch.setattr(orch_mod, "check_question_relevance", _fake_guard)
    monkeypatch.setattr(orch_mod, "get_subject_name", lambda: "Fluid Mechanics")

    orch = Orchestrator(cfg=None)
    orch._check_question_relevance("What is Bernoulli's equation?")

    assert captured["subject"] == "Fluid Mechanics"
