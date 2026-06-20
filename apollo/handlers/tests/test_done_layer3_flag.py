"""WU-5A2 — the Layer-3 belief-persist flag helper (unit, no DB / no LLM).

`APOLLO_GRAPH_SIM_LAYER3_ENABLED` gates the `run_learner_update` call inside
`handle_done`. It defaults OFF EVERYWHERE (prod + staging + test) — flipping it
ON is a later human calibration decision. These pin the env-var NAME and the
truthy/falsy parsing (mirrors the shadow/live flag helpers).
"""

from __future__ import annotations

import pytest

from apollo.handlers import done as done_mod
from apollo.handlers.done import _graph_sim_layer3_enabled

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", raising=False)
    yield


def test_graph_sim_layer3_flag_parsing(monkeypatch):
    for truthy in ("1", "true", "TRUE", "Yes", "yes"):
        monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", truthy)
        assert _graph_sim_layer3_enabled() is True
    for falsy in ("0", "false", "no", "", "off", "maybe"):
        monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", falsy)
        assert _graph_sim_layer3_enabled() is False


def test_graph_sim_layer3_default_off(monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", raising=False)
    assert _graph_sim_layer3_enabled() is False


def test_layer3_flag_constant_name():
    """Pin the env-var name so prod/test config keys match the spec."""
    assert done_mod._GRAPH_SIM_LAYER3_FLAG == "APOLLO_GRAPH_SIM_LAYER3_ENABLED"
