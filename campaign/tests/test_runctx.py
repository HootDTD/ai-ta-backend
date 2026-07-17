"""Tests for campaign.runctx.RunContext: campaign/out/<run_id>/ scaffolding,
tune-phase freeze, and gate-phase divergence refusal.
"""

from __future__ import annotations

import os

import pytest

from campaign.config import CampaignConfig, ConfigDivergedError, load_frozen
from campaign.runctx import RunContext


@pytest.fixture(autouse=True)
def _clean_config_sha_env(monkeypatch):
    monkeypatch.delenv("APOLLO_CONFIG_SHA", raising=False)
    yield


def test_create_rejects_bad_phase(tmp_path):
    with pytest.raises(ValueError, match="phase must be"):
        RunContext.create("run-1", "bogus", out_root=tmp_path)


def test_tune_phase_creates_expected_layout(tmp_path):
    ctx = RunContext.create("run-tune-1", "tune", out_root=tmp_path)

    assert ctx.out_dir == tmp_path / "run-tune-1"
    assert ctx.config_path.exists()
    assert ctx.attempts_path.exists()
    assert ctx.artifacts_dir.is_dir()
    assert ctx.config_sha

    loaded_cfg, loaded_sha = load_frozen(ctx.config_path)
    assert loaded_sha == ctx.config_sha
    assert loaded_cfg == CampaignConfig.capture_live()


def test_tune_phase_exports_config_sha_env_for_the_driver(tmp_path):
    ctx = RunContext.create("run-tune-env", "tune", out_root=tmp_path)

    assert os.environ["APOLLO_CONFIG_SHA"] == ctx.config_sha


def test_tune_phase_does_not_clobber_existing_attempts_log(tmp_path):
    ctx1 = RunContext.create("run-tune-2", "tune", out_root=tmp_path)
    ctx1.attempts_path.write_text('{"attempt": 1}\n', encoding="utf-8")

    ctx2 = RunContext.create("run-tune-2", "tune", out_root=tmp_path)

    assert ctx2.attempts_path.read_text(encoding="utf-8") == '{"attempt": 1}\n'


def test_gate_phase_requires_a_prior_frozen_config(tmp_path):
    with pytest.raises(FileNotFoundError):
        RunContext.create("run-gate-missing", "gate", out_root=tmp_path)


def test_gate_phase_succeeds_when_live_env_matches_frozen_snapshot(tmp_path):
    tune_ctx = RunContext.create("run-gate-ok", "tune", out_root=tmp_path)

    gate_ctx = RunContext.create("run-gate-ok", "gate", out_root=tmp_path)

    assert gate_ctx.config_sha == tune_ctx.config_sha
    assert gate_ctx.attempts_path.exists()


def test_gate_phase_refuses_to_run_when_live_env_diverges(tmp_path, monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    RunContext.create("run-gate-bad", "tune", out_root=tmp_path)

    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "1")
    with pytest.raises(ConfigDivergedError):
        RunContext.create("run-gate-bad", "gate", out_root=tmp_path)
