from unittest.mock import MagicMock, patch

from apollo.overseer.diagnostic import generate_diagnostic


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
        coverage={"continuity": "missing", "bernoulli": "covered", "incompressibility": "covered"},
        solver_result={"status": "stuck", "missing_variables": ["v2"]},
        reference_steps=[],
        problem_text="water in a horizontal pipe…",
    )
    assert isinstance(text, str)
    assert len(text) > 0


@patch("apollo.overseer.diagnostic.OpenAI")
def test_diagnostic_prompt_includes_coverage_and_problem(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client

    generate_diagnostic(
        coverage={"continuity": "missing"},
        solver_result={"status": "stuck", "missing_variables": ["v2"]},
        reference_steps=[],
        problem_text="SENTINEL_PROBLEM_TEXT",
    )
    called = client.chat.completions.create.call_args
    joined = " ".join(m["content"] for m in called.kwargs["messages"])
    assert "SENTINEL_PROBLEM_TEXT" in joined
    assert "missing" in joined.lower()
