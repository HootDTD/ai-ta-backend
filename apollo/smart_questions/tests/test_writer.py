import json
from types import SimpleNamespace

from apollo.smart_questions import writer


def _client(reply: str, captured: dict):
    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def test_writer_prompt_carries_nudge_and_public_context_only(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(writer, "OpenAI", lambda: _client("Why does it occur?", captured))
    result = writer.write_question(
        nudge="the problem asks why it occurs and the student has not explained that",
        problem_text="What is Future Shock, and why does it occur?",
        transcript=[("student", "future shock is when things happen too quickly")],
    )
    assert result == "Why does it occur?"
    payload = json.loads(captured["messages"][1]["content"])
    assert payload == {
        "nudge": "the problem asks why it occurs and the student has not explained that",
        "problem": "What is Future Shock, and why does it occur?",
        "student_words": ["future shock is when things happen too quickly"],
    }
    system = captured["messages"][0]["content"]
    assert "confused-student" in system
    assert "never introduce" in system.casefold()


def test_writer_has_safe_empty_fallback(monkeypatch):
    monkeypatch.setattr(writer, "OpenAI", lambda: _client("", {}))
    assert "missing one step" in writer.write_question(
        nudge="ask about the missing part",
        problem_text="Explain x.",
        transcript=[],
    )
