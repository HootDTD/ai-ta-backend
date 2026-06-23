"""Unit tests for the pure helpers of scripts/_macro_mine.py (the RAG
relevance-pathway driver). The retrieval/DB body (run()) is integration-shaped
and exercised by the live macro probe run, not here."""
from __future__ import annotations

import importlib

import pytest

mine = importlib.import_module("scripts._macro_mine")


class TestIsRelevant:
    def test_hits_on_concept_term(self) -> None:
        assert mine._is_relevant("real_gdp_from_deflator", "The GDP deflator is a price index.") is True

    def test_case_insensitive(self) -> None:
        assert mine._is_relevant("nnp_chain", "We subtract DEPRECIATION from GNP.") is True

    def test_miss_when_off_topic(self) -> None:
        assert mine._is_relevant("gdp_identity", "Bernoulli's equation for fluid flow.") is False

    def test_unknown_problem_id_never_relevant(self) -> None:
        assert mine._is_relevant("not_a_problem", "consumption investment government") is False


class TestDbUrlGuard:
    def test_rejects_remote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPABASE_DB_URL", "postgresql+asyncpg://u:p@db.prod.supabase.co:5432/postgres")
        with pytest.raises(SystemExit):
            mine._db_url()

    def test_accepts_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        local = "postgresql+asyncpg://postgres:postgres@127.0.0.1:54322/postgres"
        monkeypatch.setenv("SUPABASE_DB_URL", local)
        assert mine._db_url() == local

    def test_missing_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
        with pytest.raises(SystemExit):
            mine._db_url()


class TestLoadQuestions:
    def test_loads_the_five_authored_macro_problems(self) -> None:
        qs = mine._load_questions()
        ids = {q["id"] for q in qs}
        assert {
            "gdp_identity", "net_exports_sign", "nnp_chain",
            "real_gdp_from_deflator", "real_gdp_growth",
        } <= ids
        for q in qs:
            assert q["problem_text"] and q["concept"]
