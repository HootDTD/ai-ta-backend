"""Deterministic test doubles for external services.

These replace network-bound dependencies (OpenAI embeddings/chat) with
seeded, reproducible fakes so unit and integration tests never touch the
network and always produce identical results across runs and machines.

Phase 1 of docs/TESTING-CI-PLAN.md.
"""
from __future__ import annotations

from tests.fakes.embeddings import EMBEDDING_DIM, fake_embedding, one_hot_embedding
from tests.fakes.openai_client import FakeOpenAI

__all__ = [
    "EMBEDDING_DIM",
    "fake_embedding",
    "one_hot_embedding",
    "FakeOpenAI",
]
