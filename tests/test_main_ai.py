"""Tests for backend.main_ai core pipeline functions."""
from __future__ import annotations

import json
import types

import backend.main_ai as mai


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
# _client caching
# ---------------------------------------------------------------------------

def test_client_caching(monkeypatch):
    """_client() should return the same instance on repeated calls."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = None  # reset

    c1 = mai._client()
    c2 = mai._client()
    assert c1 is c2

    mai._cached_client = None  # cleanup


# ---------------------------------------------------------------------------
# normalize_query
# ---------------------------------------------------------------------------

def test_normalize_query_basic():
    assert mai.normalize_query("") == ""
    result = mai.normalize_query("  What is  Lift?  ")
    assert "what" in result
    assert "lift" in result
    # No double spaces
    assert "  " not in result


def test_normalize_query_curly_quotes():
    result = mai.normalize_query("\u201cBernoulli\u2019s equation\u201d")
    assert "\u201c" not in result  # no curly quotes
    assert "\u2019" not in result


# ---------------------------------------------------------------------------
# _score_and_answer_snippet
# ---------------------------------------------------------------------------

def test_score_and_answer_snippet_parses_json(monkeypatch):
    """Verify merged score+answer parses a well-formed JSON response."""
    fake_response = {
        "context": "Relates to boundary layer thickness",
        "relevance": 0.85,
        "directness": 0.9,
        "score": 0.87,
        "answer": "The boundary layer grows as sqrt(x).",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake_response)

    snippet = types.SimpleNamespace(
        id="s1", text="boundary layer", page=5,
        section_path="ch3/sec1", why="relevant", citation_marker="[S1]",
    )

    result = mai._score_and_answer_snippet(
        question="How does the boundary layer grow?",
        snippet=snippet,
        importance=1.0,
        focus_term="boundary",
    )

    assert result["marker"] == "[S1]"
    assert result["snippet_id"] == "s1"
    assert 0 <= result["relevance"] <= 1
    assert 0 <= result["score"] <= 1
    assert "boundary layer" in result["answer"].lower()
    assert result["context"] != ""

    mai._cached_client = None  # cleanup


def test_score_and_answer_snippet_fallback_on_error(monkeypatch):
    """When the LLM call fails, should return a fallback with score near 0."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    class _FailClient:
        chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(
                create=staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("fail")))
            )
        )

    mai._cached_client = _FailClient()

    snippet = types.SimpleNamespace(
        id="s2", text="some text", page=1,
        section_path="", why="", citation_marker=None,
    )

    result = mai._score_and_answer_snippet(
        question="test question",
        snippet=snippet,
        importance=1.0,
        focus_term="test",
    )

    # Should still return a dict with all required keys
    assert "marker" in result
    assert "score" in result
    assert "answer" in result
    # Score should be 0 since LLM failed (data = {})
    assert result["relevance"] == 0.0

    mai._cached_client = None  # cleanup


# ---------------------------------------------------------------------------
# extract_and_filter_keywords
# ---------------------------------------------------------------------------

def test_extract_and_filter_keywords_parses_response(monkeypatch):
    """Verify merged keyword extraction parses a well-formed JSON response."""
    fake_response = {
        "context_summary": "boundary layer, Reynolds number",
        "ranked_terms": [
            {"term": "boundary", "relevance": 0.95},
            {"term": "reynolds", "relevance": 0.90},
            {"term": "turbulence", "relevance": 0.70},
        ],
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mai._cached_client = _make_fake_client(fake_response)
    monkeypatch.setattr(mai, "get_subject_name", lambda: "Fluid Mechanics")

    summary, terms = mai.extract_and_filter_keywords(
        "What is the boundary layer Reynolds number?"
    )

    assert "boundary" in summary.lower() or "reynolds" in summary.lower()
    assert len(terms) == 3
    assert terms[0]["term"] == "boundary"
    assert terms[0]["relevance"] == 0.95

    mai._cached_client = None  # cleanup


def test_extract_and_filter_keywords_fallback_on_error(monkeypatch):
    """When the LLM call fails, should return question as summary and empty list."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    class _FailClient:
        chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(
                create=staticmethod(lambda **kw: (_ for _ in ()).throw(RuntimeError("fail")))
            )
        )

    mai._cached_client = _FailClient()
    monkeypatch.setattr(mai, "get_subject_name", lambda: "Test Subject")

    summary, terms = mai.extract_and_filter_keywords("test question about drag")

    assert summary == "test question about drag"
    assert terms == []

    mai._cached_client = None  # cleanup
