import types

import backend.retriever as r
from backend.retriever import ContextSnippet, ContextPack


def make_dummy_client(text: str):
    class Dummy:
        @staticmethod
        def create(**kwargs):
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content=text)
                    )
                ]
            )

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=Dummy))


def test_answer_strips_citation_markers(monkeypatch):
    monkeypatch.setattr(r, "_require_loaded", lambda: None)
    monkeypatch.setattr(r, "_get_client", lambda: make_dummy_client("result [S1] and [S2]"))
    r._meta = {}
    sn1 = ContextSnippet(
        id="1",
        type="text",
        page=1,
        section_path="sec1",
        text="snippet1",
        figure_id=None,
        why="",
        source_path="src1",
        doc_title=None,
        doc_short="doc1",
    )
    sn2 = ContextSnippet(
        id="2",
        type="text",
        page=2,
        section_path="sec2",
        text="snippet2",
        figure_id=None,
        why="",
        source_path="src2",
        doc_title=None,
        doc_short="doc2",
    )
    ctx = ContextPack(snippets=[sn1, sn2], used_ids=[], stats={})
    ans = r.answer("question", ctx)
    assert "[S1]" not in ans.text and "[S2]" not in ans.text
    assert "[§" not in ans.text
    assert len(ans.citations) == 2
