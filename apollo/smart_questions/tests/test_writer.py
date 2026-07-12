from types import SimpleNamespace

from apollo.ontology import build_node
from apollo.smart_questions import writer


def test_writer_passes_private_target_and_returns_one_question(monkeypatch):
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content="Why does that step work?"))
                ]
            )

    monkeypatch.setattr(
        writer,
        "OpenAI",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )
    node = build_node(
        node_type="definition",
        node_id="a",
        attempt_id=1,
        source="reference",
        content={"concept": "pressure", "meaning": "private answer"},
    )
    result = writer.write_question(node=node, transcript=[("student", "I used pressure")])
    assert result == "Why does that step work?"
    assert "private_target" in captured["messages"][1]["content"]
    assert "Never state" in captured["messages"][0]["content"]


def test_writer_has_safe_empty_fallback(monkeypatch):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])

    monkeypatch.setattr(
        writer, "OpenAI", lambda: SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    )
    node = build_node(
        node_type="definition",
        node_id="a",
        attempt_id=1,
        source="reference",
        content={"concept": "x", "meaning": "y"},
    )
    assert "missing one step" in writer.write_question(node=node, transcript=[])


def test_writer_rejects_unintroduced_private_answer(monkeypatch):
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content="Does it mean private answer?"))
                ]
            )

    monkeypatch.setattr(
        writer, "OpenAI", lambda: SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    )
    node = build_node(
        node_type="definition",
        node_id="a",
        attempt_id=1,
        source="reference",
        content={"concept": "pressure", "meaning": "private answer"},
    )
    assert "missing one step" in writer.write_question(
        node=node, transcript=[("student", "I used pressure")]
    )
