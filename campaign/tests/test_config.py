"""Tests for campaign.config: snapshot/freeze/reload of every campaign tunable.

Covers: snapshot round-trip, config_sha stability + tamper detection, and the
gate-phase divergence guard (campaign.runctx tests exercise the RunContext
wiring; here we test the pure config-layer primitives).
"""

from __future__ import annotations

import json

import pytest

from campaign.config import (
    CampaignConfig,
    ConfigDivergedError,
    assert_live_matches_frozen,
    config_sha,
    freeze,
    load_frozen,
    snapshot_flags,
)


def test_capture_live_reflects_current_defaults(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_CLARIFICATION_ENABLED", raising=False)
    cfg = CampaignConfig.capture_live()

    assert cfg.flags["APOLLO_NLI_ENABLED"] is True  # default-on
    assert cfg.flags["APOLLO_CLARIFICATION_ENABLED"] is False  # default-off
    assert set(cfg.axis_weights.keys()) == {
        "procedure",
        "justification",
        "simplification",
        "misconception_corrected",
    }
    assert cfg.letter_bands[0] == (97, "A+")
    assert cfg.abstention_thresholds["unresolved_rate"] == pytest.approx(0.35)
    assert cfg.nli_model  # non-empty
    assert cfg.nli_params.min_entailment > 0


def test_snapshot_flags_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "1")
    monkeypatch.setenv("APOLLO_NLI_ENABLED", "0")

    flags = snapshot_flags()

    assert flags["APOLLO_CLARIFICATION_ENABLED"] is True
    assert flags["APOLLO_NLI_ENABLED"] is False


def test_snapshot_round_trips_through_from_snapshot(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    cfg = CampaignConfig.capture_live()

    restored = CampaignConfig.from_snapshot(cfg.snapshot())

    assert restored == cfg
    assert restored.snapshot() == cfg.snapshot()


def test_config_sha_is_stable_for_identical_snapshot(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    cfg = CampaignConfig.capture_live()

    sha_a = config_sha(cfg.snapshot())
    sha_b = config_sha(CampaignConfig.from_snapshot(cfg.snapshot()).snapshot())

    assert sha_a == sha_b
    assert len(sha_a) == 64  # sha256 hex digest


def test_config_sha_changes_when_a_tunable_changes(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    cfg = CampaignConfig.capture_live()
    sha_before = config_sha(cfg.snapshot())

    monkeypatch.setenv("APOLLO_NLI_MIN_ENTAILMENT", "0.42")
    cfg_after = CampaignConfig.capture_live()
    sha_after = config_sha(cfg_after.snapshot())

    assert sha_before != sha_after


def test_freeze_then_load_frozen_round_trips(tmp_path, monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    cfg = CampaignConfig.capture_live()
    path = tmp_path / "nested" / "config.json"

    frozen = freeze(cfg, path)

    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["config_sha"] == frozen["config_sha"]

    loaded_cfg, loaded_sha = load_frozen(path)

    assert loaded_cfg == cfg
    assert loaded_sha == frozen["config_sha"]


def test_load_frozen_rejects_tampered_file(tmp_path, monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    cfg = CampaignConfig.capture_live()
    path = tmp_path / "config.json"
    freeze(cfg, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["config"]["axis_weights"]["procedure"] = 0.99
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="config_sha mismatch"):
        load_frozen(path)


def test_assert_live_matches_frozen_passes_when_unchanged(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    frozen_cfg = CampaignConfig.capture_live()

    assert_live_matches_frozen(frozen_cfg)  # must not raise


def test_assert_live_matches_frozen_raises_on_divergence(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_ENABLED", raising=False)
    frozen_cfg = CampaignConfig.capture_live()

    monkeypatch.setenv("APOLLO_NLI_MIN_ENTAILMENT", "0.01")

    with pytest.raises(ConfigDivergedError):
        assert_live_matches_frozen(frozen_cfg)
