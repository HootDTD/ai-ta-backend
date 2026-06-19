"""WU-5B5 — LIVE /ask chat-keyword wire-up (integration, through the /ask handler).

Reuses the ``client_with_server`` pattern from ``test_server_part8.py``
(``TEST_FAKE_OPENAI=1``, in-memory sqlite). Every pipeline stage is monkeypatched
so no LLM / network / real DB is touched; ``_retrieve_bundle_with_router`` returns a
fake bundle carrying ``found_terms``, and ``_append_assistant_turn_and_refresh`` is
spied to capture the ``keywords=`` kwarg the live call site computes.

Locks (§10 RQ5 hedge, live path):
- non-streaming /ask threads ``bundle.found_terms`` -> ``keywords=`` (Edit 4);
- a bundle without ``found_terms`` -> ``keywords == []``;
- NO behavior change: the /ask answer + citations payload is byte-identical;
- the user-turn append carries NO ``keywords`` (assistant turn only).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from auth import AuthContext
from config.contracts import ParsedTask


@pytest.fixture
def client_with_server(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")

    import server as server

    with TestClient(server.app) as client:
        yield client, server


def _wire_ask_pipeline(server, monkeypatch, *, bundle, assistant_turns):
    """Monkeypatch every /ask pipeline stage exactly like test_server_part8 does,
    returning the supplied fake ``bundle`` from retrieval and recording every
    ``_append_assistant_turn_and_refresh`` kwargs dict into ``assistant_turns``."""
    fake_workspace = SimpleNamespace(
        class_name="AAE 33300",
        subject_name="Fluid Mechanics",
        weight_overrides={},
        materials=[],
        metadata={"search_space_id": 1},
    )

    class _WorkspaceManager:
        def get(self, identifier: str):
            return fake_workspace

    class _TeacherStorage:
        def get_retrieval_weights_by_search_space(self, search_space_id: int):
            return {}

    monkeypatch.setattr(
        server,
        "_require_course_membership",
        lambda _request, *, search_space_id, role=None: AuthContext(
            user_id="student-1", access_token="tok"
        ),
    )
    monkeypatch.setattr(server, "_get_workspace_manager", lambda: _WorkspaceManager())
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: _TeacherStorage())
    monkeypatch.setattr(server, "_save_attachments", lambda _atts: [])
    monkeypatch.setattr(
        server,
        "_load_memory_and_append_user_turn",
        lambda **kwargs: "",
    )
    # Retrieval returns the fake bundle carrying found_terms; this is the object the
    # live call site passes to _keywords_from_bundle(bundle).
    monkeypatch.setattr(
        server,
        "_retrieve_bundle_with_router",
        lambda **kwargs: (bundle, 0),
    )
    monkeypatch.setattr(
        server,
        "parse_question",
        lambda *args, **kwargs: ParsedTask(
            problem_type="qa", asked_outputs=["answer"], asked_output_keys=["answer"]
        ),
    )
    monkeypatch.setattr(server, "solve_with_bundle", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        server,
        "format_answer",
        lambda *args, **kwargs: SimpleNamespace(text="Memory-aware answer", citations=[]),
    )
    monkeypatch.setattr(
        server,
        "_structured_citations_from_bundle",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        server,
        "_append_assistant_turn_and_refresh",
        lambda **kwargs: assistant_turns.append(kwargs),
    )


def _bundle_with(found_terms):
    return SimpleNamespace(
        found_terms=list(found_terms),
        metadata=SimpleNamespace(found_terms=[]),
        snippets=[],
    )


def _post_ask(client, chat_id="chat-1"):
    return client.post(
        "/ask",
        json={
            "question": "How do we compute dynamic pressure?",
            "chat_id": chat_id,
            "search_space_id": 1,
            "attachments": [],
        },
    )


@pytest.mark.integration
def test_ask_happy_path_persists_bundle_found_terms(client_with_server, monkeypatch):
    client, server = client_with_server
    assistant_turns: list[dict] = []
    _wire_ask_pipeline(
        server,
        monkeypatch,
        bundle=_bundle_with(["bernoulli", "pressure"]),
        assistant_turns=assistant_turns,
    )

    resp = _post_ask(client)

    assert resp.status_code == 200, resp.text
    assert assistant_turns, "assistant turn should be persisted"
    assert assistant_turns[0]["keywords"] == ["bernoulli", "pressure"]


@pytest.mark.integration
def test_ask_bundle_without_found_terms_persists_empty(client_with_server, monkeypatch):
    client, server = client_with_server
    assistant_turns: list[dict] = []
    _wire_ask_pipeline(
        server,
        monkeypatch,
        bundle=_bundle_with([]),  # NONE / cache shape: no found_terms
        assistant_turns=assistant_turns,
    )

    resp = _post_ask(client)

    assert resp.status_code == 200, resp.text
    assert assistant_turns
    assert assistant_turns[0]["keywords"] == []


@pytest.mark.integration
def test_ask_no_behavior_change_answer_and_citations_unchanged(client_with_server, monkeypatch):
    client, server = client_with_server
    assistant_turns: list[dict] = []
    user_turn_keywords: list = []

    _wire_ask_pipeline(
        server,
        monkeypatch,
        bundle=_bundle_with(["bernoulli", "pressure"]),
        assistant_turns=assistant_turns,
    )
    # Belt-and-suspenders: spy the user-turn append to prove it carries no keywords.
    monkeypatch.setattr(
        server,
        "_load_memory_and_append_user_turn",
        lambda **kwargs: user_turn_keywords.append(kwargs.get("keywords", "ABSENT")) or "",
    )

    resp = _post_ask(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Golden snapshot: threading keywords must not perturb the user-visible payload.
    assert body["answer"] == "Memory-aware answer"
    assert body["citations"] == []
    # The user-turn append never receives a keywords kwarg.
    assert user_turn_keywords == ["ABSENT"]


def _wire_stream_pipeline(server, monkeypatch, *, bundle, assistant_turns):
    """Monkeypatch every /ask/stream stage so the SSE generator reaches the
    assistant-turn persist call site (Edit 5, server.py streaming happy path).
    router_ctx is forced to None so the router-outcome persist branch is skipped."""
    fake_workspace = SimpleNamespace(
        class_name="AAE 33300",
        subject_name="Fluid Mechanics",
        weight_overrides={},
        materials=[],
        metadata={"search_space_id": 1},
    )

    class _WorkspaceManager:
        def get(self, identifier: str):
            return fake_workspace

    monkeypatch.setattr(
        server,
        "_require_course_membership",
        lambda _request, *, search_space_id, role=None: AuthContext(
            user_id="student-1", access_token="tok"
        ),
    )
    monkeypatch.setattr(server, "_get_workspace_manager", lambda: _WorkspaceManager())
    monkeypatch.setattr(server, "_get_teacher_storage", lambda: None)
    monkeypatch.setattr(server, "_save_attachments", lambda _atts: [])
    monkeypatch.setattr(server, "_load_memory_and_append_user_turn", lambda **kwargs: "")
    monkeypatch.setattr(server, "_prepare_router_context_sync", lambda **kwargs: None)
    monkeypatch.setattr(server, "_build_retrieval_weight_overrides", lambda **kwargs: {})
    monkeypatch.setattr(server, "_retrieve_bundle_with_router", lambda **kwargs: (bundle, 0))
    monkeypatch.setattr(
        server,
        "parse_question",
        lambda *args, **kwargs: ParsedTask(
            problem_type="qa", asked_outputs=["answer"], asked_output_keys=["answer"]
        ),
    )

    def _fake_stream(*args, **kwargs):
        yield ("solution", SimpleNamespace())

    monkeypatch.setattr(server, "solve_with_bundle_stream", _fake_stream)
    monkeypatch.setattr(server, "solve_with_bundle", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        server,
        "format_answer",
        lambda *args, **kwargs: SimpleNamespace(text="Streamed answer", citations=[]),
    )
    monkeypatch.setattr(server, "_structured_citations_from_bundle", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        server,
        "_append_assistant_turn_and_refresh",
        lambda **kwargs: assistant_turns.append(kwargs),
    )


@pytest.mark.integration
def test_ask_streaming_happy_path_persists_found_terms(client_with_server, monkeypatch):
    client, server = client_with_server
    assistant_turns: list[dict] = []
    _wire_stream_pipeline(
        server,
        monkeypatch,
        bundle=_bundle_with(["momentum", "impulse"]),
        assistant_turns=assistant_turns,
    )

    with client.stream(
        "POST",
        "/ask/stream",
        json={
            "question": "Explain momentum.",
            "chat_id": "chat-stream-1",
            "search_space_id": 1,
            "attachments": [],
        },
    ) as resp:
        assert resp.status_code == 200
        # Drain the SSE stream so the generator runs to completion (the persist
        # call site executes after the "answer" event is yielded).
        body = "".join(resp.iter_text())

    assert "Streamed answer" in body
    assert assistant_turns, "streaming assistant turn should be persisted"
    assert assistant_turns[0]["keywords"] == ["momentum", "impulse"]
