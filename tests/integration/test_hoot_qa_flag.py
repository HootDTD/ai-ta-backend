"""HOOT_QA_ENABLED kill switch: Apollo-only deployments close POST /ask.

The MGMT 38200 pilot runs prod with the Hoot Q&A surface off (flag=0) while
staging keeps it on (default). The gate sits BEFORE payload validation, so a
disabled deployment 403s even on garbage input; an enabled one proceeds to
validation (400 on an empty payload proves the gate passed).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
    monkeypatch.setenv("SUPABASE_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("TEST_FAKE_OPENAI", "1")

    import server

    with TestClient(server.app) as c:
        yield c


_VALID_SHAPE = {"question": "", "chat_id": "c1", "search_space_id": 1}


def test_ask_403_when_hoot_qa_disabled(client, monkeypatch):
    monkeypatch.setenv("HOOT_QA_ENABLED", "0")
    resp = client.post("/ask", json=_VALID_SHAPE)
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"]


def test_ask_passes_gate_when_enabled_default(client, monkeypatch):
    monkeypatch.delenv("HOOT_QA_ENABLED", raising=False)
    # Empty question + no attachments → 400 from the endpoint's own validation,
    # proving the 403 gate did not fire.
    resp = client.post("/ask", json=_VALID_SHAPE)
    assert resp.status_code == 400


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_flag_off_spellings(value, monkeypatch):
    from config.settings import hoot_qa_enabled

    monkeypatch.setenv("HOOT_QA_ENABLED", value)
    assert hoot_qa_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "", "anything"])
def test_flag_on_spellings(value, monkeypatch):
    from config.settings import hoot_qa_enabled

    monkeypatch.setenv("HOOT_QA_ENABLED", value)
    assert hoot_qa_enabled() is True
