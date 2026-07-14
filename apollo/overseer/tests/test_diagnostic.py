import inspect
from unittest.mock import MagicMock, patch

from apollo.overseer import diagnostic
from apollo.overseer.diagnostic import generate_diagnostic


def test_generate_diagnostic_has_no_solver_param():
    sig = inspect.signature(diagnostic.generate_diagnostic)
    assert "solver_result" not in sig.parameters


def test_generate_diagnostic_narrates_without_solver(monkeypatch):
    captured = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    captured.update(kwargs)

                    class _M:
                        content = "You taught the continuity step clearly. Next, walk Apollo through the pressure relationship."

                    class _C:
                        message = _M()

                    class _R:
                        choices = [_C()]

                    return _R()

    monkeypatch.setattr(diagnostic, "OpenAI", _Client)

    out = diagnostic.generate_diagnostic(
        coverage={"per_step": {"bernoulli": "missing"}, "procedure_scores": {}, "confidences": {}},
        reference_steps=[{"id": "bernoulli", "entry_type": "equation", "content": {"label": "Bernoulli"}}],
        problem_text="Water flows through a horizontal pipe...",
        rubric={"overall": {"score": 0.5, "letter": "C"}},
    )
    assert "continuity" in out
    # The solver framing must be gone from the system prompt.
    system = captured["messages"][0]["content"]
    assert "solver" not in system.lower()


def _mock_reply(text: str) -> MagicMock:
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=text))]
    return fake


@patch("apollo.overseer.diagnostic.OpenAI")
def test_diagnostic_returns_string(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("You taught Bernoulli well but missed continuity.")
    mock_client_cls.return_value = client

    text = generate_diagnostic(
        coverage={"per_step": {"continuity": "missing", "bernoulli": "covered", "incompressibility": "covered"}, "procedure_scores": {}},
        reference_steps=[],
        problem_text="water in a horizontal pipe…",
        rubric={
            "overall": {"score": 100, "letter": "A+"},
            "procedure": {"score": 100, "letter": "A+", "present": True},
            "justification": {"score": 100, "letter": "A+", "present": True},
            "simplification": {"score": 100, "letter": "A+", "present": True},
        },
    )
    assert isinstance(text, str)
    assert len(text) > 0


@patch("apollo.overseer.diagnostic.OpenAI")
def test_diagnostic_prompt_includes_coverage_and_problem(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client

    generate_diagnostic(
        coverage={"per_step": {"continuity": "missing"}, "procedure_scores": {}},
        reference_steps=[],
        problem_text="SENTINEL_PROBLEM_TEXT",
        rubric={
            "overall": {"score": 80, "letter": "B+"},
            "procedure": {"score": 100, "letter": "A+", "present": True},
            "justification": {"score": 100, "letter": "A+", "present": True},
            "simplification": {"score": 100, "letter": "A+", "present": True},
        },
    )
    called = client.chat.completions.create.call_args
    joined = " ".join(m["content"] for m in called.kwargs["messages"])
    assert "SENTINEL_PROBLEM_TEXT" in joined
    assert "missing" in joined.lower()


@patch("apollo.overseer.diagnostic.OpenAI")
def test_generate_diagnostic_passes_rubric_into_llm(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="narrative"))]
    )
    mock_client_cls.return_value = client

    rubric = {
        "overall": {"score": 78, "letter": "B+"},
        "procedure": {"score": 60, "letter": "C+", "present": True},
        "justification": {"score": 100, "letter": "A", "present": True},
        "simplification": {"score": 100, "letter": "A", "present": True},
    }
    generate_diagnostic(
        coverage={"per_step": {"p1": "missing"}, "procedure_scores": {"p1": 0.3}},
        reference_steps=[{"id": "p1", "entry_type": "procedure_step", "content": {"action": "x", "order": 1}}],
        problem_text="Demo problem.",
        rubric=rubric,
    )
    called = client.chat.completions.create.call_args
    user_msg = next(m for m in called.kwargs["messages"] if m["role"] == "user")
    assert "B+" in user_msg["content"]
    assert "procedure" in user_msg["content"].lower()
    assert "78" in user_msg["content"]


@patch("apollo.overseer.diagnostic.OpenAI")
def test_generate_diagnostic_system_prompt_instructs_narrative_not_verdict(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="narrative"))]
    )
    mock_client_cls.return_value = client

    generate_diagnostic(
        coverage={"per_step": {}, "procedure_scores": {}},
        reference_steps=[],
        problem_text="Demo.",
        rubric={
            "overall": {"score": 0, "letter": "F"},
            "procedure": {"score": 0, "letter": "F", "present": False},
            "justification": {"score": 0, "letter": "F", "present": False},
            "simplification": {"score": 0, "letter": "F", "present": False},
        },
    )
    called = client.chat.completions.create.call_args
    system_msg = next(m for m in called.kwargs["messages"] if m["role"] == "system")
    sys_lower = " ".join(system_msg["content"].lower().split())
    # The legacy lane follows the same direct-to-student coaching contract.
    assert "rubric" in sys_lower
    assert "address the student only as" in sys_lower
    assert '"you" and "your' in sys_lower
    assert 'never say "the student"' in sys_lower
    assert "prioritize at most two" in sys_lower
    assert "never say that no misconceptions" in sys_lower
    assert "do not re-grade" in sys_lower


@patch("apollo.overseer.diagnostic.OpenAI")
def test_generate_diagnostic_softfails_to_placeholder_on_llm_exception(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("network down")
    mock_client_cls.return_value = client

    result = generate_diagnostic(
        coverage={"per_step": {}, "procedure_scores": {}},
        reference_steps=[],
        problem_text="Demo.",
        rubric={
            "overall": {"score": 0, "letter": "F"},
            "procedure": {"score": 0, "letter": "F", "present": False},
            "justification": {"score": 0, "letter": "F", "present": False},
            "simplification": {"score": 0, "letter": "F", "present": False},
        },
    )
    assert "unavailable" in result.lower()
    # The rubric is still accurate even if narrative fails.
    assert "grade" in result.lower() or "still accurate" in result.lower()
