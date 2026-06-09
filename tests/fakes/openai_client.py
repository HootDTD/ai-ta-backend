"""A drop-in fake for `openai.OpenAI` used in tests.

Mirrors the slices of the OpenAI SDK surface the backend actually calls:

    client.chat.completions.create(...).choices[0].message.content
    client.embeddings.create(input=..., dimensions=...).data[i].embedding

Chat returns a configurable canned JSON payload (the backend expects
structured JSON from the LLM layer — never raw text). Embeddings are produced
by :func:`tests.fakes.embeddings.fake_embedding`, so they are deterministic and
the correct dimension for the real `Vector` columns.
"""

from __future__ import annotations

import types
from typing import Any

from tests.fakes.embeddings import EMBEDDING_DIM, fake_embedding

DEFAULT_CHAT_JSON = '{"markdown":"# Stub report","jsonld":{"@type":"Report"}}'


class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=self._content))]
        )


class _FakeEmbeddings:
    def create(self, *args: Any, input: Any = None, **kwargs: Any) -> Any:
        dim = int(kwargs.get("dimensions") or EMBEDDING_DIM)
        items = input if isinstance(input, (list, tuple)) else [input]
        data = [
            types.SimpleNamespace(embedding=fake_embedding(str(text), dim=dim)) for text in items
        ]
        return types.SimpleNamespace(data=data)


class FakeOpenAI:
    """Constructor-compatible stand-in for ``openai.OpenAI``."""

    def __init__(self, *args: Any, chat_json: str = DEFAULT_CHAT_JSON, **kwargs: Any) -> None:
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(chat_json))
        self.embeddings = _FakeEmbeddings()
