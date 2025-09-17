import json
import types

from backend.contracts import (
    BundleSnippet,
    ProposedSolution,
    ResearchBundle,
    ResearchMetadata,
)
from backend.main_ai import format_answer
from backend.qa import cmd_ask
from backend.retriever import Answer


def _make_bundle(markers):
    snippets = []
    for i, marker in enumerate(markers, start=1):
        snippets.append(
            BundleSnippet(
                id=f"s{i}",
                type="text",
                page=10 + i,
                section_path=f"sec{i}",
                text=f"Snippet {i} text.",
                figure_id=None,
                why="seed",
                source_path=f"src{i}",
                doc_title="Doc",
                doc_short="Doc",
                citation_marker=marker,
            )
        )
    metadata = ResearchMetadata(
        doc_sets=[],
        loaded_indexes=[],
        question="q",
        allowed_markers=list(markers),
        found_terms=["term1"],
        not_found_terms=[],
        attempted_terms=["term1"],
        subject="Test Subject",
    )
    bundle = ResearchBundle(
        metadata=metadata,
        snippets=snippets,
        equations=[{"eq_text": "x = y"}],
        glossary=[{"term": "x", "definition": "value"}],
        allowed_markers=list(markers),
        found_terms=["term1"],
        not_found_terms=[],
        attempted_terms=["term1"],
        subject="Test Subject",
        used_ids=[sn.id for sn in snippets],
    )
    return bundle


def test_format_answer_adds_rotating_citations():
    bundle = _make_bundle(["[Textbook, p. 10]", "[Textbook, p. 20]"])
    solution = ProposedSolution(steps="Para one.\n\nSecond para.", final_answers={})

    final = format_answer(solution, bundle)

    paragraphs = final.text.split("\n\n")
    assert paragraphs[0].endswith("[Textbook, p. 10]")
    assert paragraphs[1].endswith("[Textbook, p. 20]")
    assert "Citations: [Textbook, p. 10], [Textbook, p. 20]" in final.text
    assert final.citations == ["[Textbook, p. 10]", "[Textbook, p. 20]"]


def test_format_answer_not_found_passthrough():
    bundle = _make_bundle(["[Textbook, p. 30]"])
    solution = ProposedSolution(
        steps="Not found in the approved materials.", final_answers={}
    )

    final = format_answer(solution, bundle)

    assert final.text == "Not found in the approved materials."
    assert final.citations == []


def test_cmd_ask_writes_proof_metadata(monkeypatch, tmp_path):
    bundle = _make_bundle(["[Textbook, p. 40]"])
    bundle.not_found_terms = ["missing"]
    bundle.metadata.not_found_terms = ["missing"]
    bundle.attempted_terms = ["term1"]
    bundle.metadata.attempted_terms = ["term1"]

    monkeypatch.chdir(tmp_path)

    import backend.qa as qa_module

    monkeypatch.setattr(qa_module, "load_assets", lambda path: None)
    monkeypatch.setattr(qa_module, "load_assets_all", lambda paths: ([], []))

    def fake_answer(question, ctx):
        return Answer(text="response", citations=[], proof={})

    monkeypatch.setattr(qa_module, "answer", fake_answer)

    class DummyOrchestrator:
        def __init__(self):
            pass

        def _iterative_research(self, question, opts, max_iters):
            return bundle

    monkeypatch.setattr(qa_module, "Orchestrator", lambda: DummyOrchestrator())

    args = types.SimpleNamespace(
        question="What is asked?",
        index=["dummy"],
        k_sem=10,
        k_lex=10,
        token_budget=1000,
        max_iters=1,
        subject=None,
    )

    cmd_ask(args)

    with open(tmp_path / "proof.json", "r", encoding="utf-8") as fh:
        data = json.load(fh)

    assert data["allowed_markers"] == ["[Textbook, p. 40]"]
    assert data["subject"] == "Test Subject"
    assert data["not_found_terms"] == ["missing"]
    assert data["attempted_terms"] == ["term1"]
