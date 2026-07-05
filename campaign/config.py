"""Campaign config snapshot/freeze (WU C3).

Captures EVERY grading tunable that participates in the composite score
pipeline so a campaign run (tune or gate) can be reproduced byte-for-byte
later: rubric axis weights + letter bands, the NLI resolver tier's tuned
params, the §6.6 abstention gate thresholds, and the boolean feature flags
that change which code path an attempt goes through.

``CampaignConfig`` is a frozen dataclass; :meth:`CampaignConfig.capture_live`
reads the CURRENT process environment + code constants. :func:`freeze` writes
a hash-stamped JSON snapshot (``config_sha`` = sha256 of the canonical JSON
encoding of the snapshot dict); :func:`load_frozen` reloads it and verifies
the hash was not tampered with. :func:`assert_live_matches_frozen` is the
gate-phase safety check (WU C3 / campaign plan Phase C): a "gate" run must
refuse to execute against an environment that has drifted from the frozen
"tune" snapshot it was calibrated against.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apollo.grading.abstention import ABSTENTION_THRESHOLDS
from apollo.overseer.rubric import AXIS_WEIGHTS, LETTER_BANDS
from apollo.resolution.nli_config import NLIParams, active_nli_model, load_nli_params
from config.settings import apollo_composite_coverage_min

#: Boolean campaign flags this config snapshots, mapped to the default each
#: flag's OWN resolver function documents (kept in sync by hand — these are
#: read-only mirrors, never used to compute the flags themselves; the real
#: default lives in each flag's module, e.g. ``nli_config.nli_enabled``).
_BOOLEAN_FLAG_DEFAULTS: dict[str, bool] = {
    "APOLLO_NLI_ENABLED": True,  # default-on: apollo.resolution.nli_config.nli_enabled
    "APOLLO_CLARIFICATION_ENABLED": False,
    "APOLLO_AUTOPROVISION_ENABLED": False,
    "APOLLO_DONE_GATE_ENABLED": False,
    "APOLLO_GRAPH_SIM_LIVE_ENABLED": False,
    "APOLLO_GRAPH_SIM_SHADOW_ENABLED": False,
    "APOLLO_LEARNER_DECAY_ENABLED": False,
    "APOLLO_LEARNER_JANITOR_ENABLED": False,
    "APOLLO_LEARNER_NEGOTIATION_ENABLED": False,
    "APOLLO_MISCONCEPTION_ENABLED": False,
    # 2026-07 misc-detection routing fixes: also captured transitively via
    # nli_params.misc_positive_certify / .misc_certify_entailment (both ride
    # along in load_nli_params()'s snapshot below), but tracked here too as a
    # direct, flag-level audit signal (mirrors the composite-gate-probe
    # "threading verification" finding — don't just trust env vars were read).
    "APOLLO_OLM_INVITES_ENABLED": False,
    "APOLLO_SESSION_PERSONALIZATION_ENABLED": False,
    "APOLLO_STRUCTURED_SCRAPE": False,
    # certify-zero-fire-trace.md audit fix: these two grading-behavior flags
    # shipped (PRs #99/#100) without a snapshot entry, so the f1c run had NO
    # audit trail that the certify flag was silently unset in the server env.
    # Every grading-behavior APOLLO_* boolean MUST be registered here.
    "APOLLO_NLI_MISC_POSITIVE_CERTIFY": False,  # config.settings.apollo_nli_misc_positive_certify
    "APOLLO_ABSTENTION_COMPOSITE": False,  # config.settings.apollo_abstention_composite_enabled
}

#: Integer campaign tunables this config snapshots, mapped to the default each
#: value's OWN reader documents (read-only mirrors, same contract as
#: ``_BOOLEAN_FLAG_DEFAULTS``; the real default lives in the owning module —
#: here ``apollo.handlers.done_grading._NLI_GRADING_NODE_CAP_DEFAULT``).
#: ``APOLLO_NLI_GRADING_MAX_NODES`` is tracked because campaign/probe runs OPT
#: IN to a raised cap (40 — see ``campaign/infra/env.campaign.example``) while
#: the prod default stays 15 (2026-07 routing Fix 1, revised after PR #101
#: review): every frozen config.json must audit which cap a run graded under.
_INT_FLAG_DEFAULTS: dict[str, int] = {
    "APOLLO_NLI_GRADING_MAX_NODES": 15,
}

#: The env var the campaign driver (D3) reads to stamp ``versions.weights_version``
#: on every artifact it writes. Set by :class:`campaign.runctx.RunContext` when a
#: run's config is frozen.
CONFIG_SHA_ENV_VAR = "APOLLO_CONFIG_SHA"


def _read_bool_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


def snapshot_flags() -> dict[str, bool]:
    """Current value of every tracked boolean flag (env override applied on
    top of that flag's documented default), sorted by name for determinism."""
    return {
        name: _read_bool_flag(name, default)
        for name, default in sorted(_BOOLEAN_FLAG_DEFAULTS.items())
    }


def _read_int_flag(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        # Mirror the owning readers' behavior (e.g. done_grading.
        # _nli_grading_node_cap): a malformed override falls back to the
        # default rather than crashing a campaign run at snapshot time.
        return default


def snapshot_int_flags() -> dict[str, int]:
    """Current value of every tracked integer tunable (env override applied on
    top of that value's documented default), sorted by name for determinism."""
    return {
        name: _read_int_flag(name, default) for name, default in sorted(_INT_FLAG_DEFAULTS.items())
    }


@dataclass(frozen=True)
class CampaignConfig:
    """Every tunable that feeds the composite grade for one campaign run."""

    axis_weights: dict[str, float]
    letter_bands: tuple[tuple[int, str], ...]
    nli_model: str
    nli_params: NLIParams
    abstention_thresholds: dict[str, float]
    flags: dict[str, bool]
    int_flags: dict[str, int] = dataclasses.field(default_factory=dict)
    # §10 composite gate threshold (APOLLO_COMPOSITE_COVERAGE_MIN). A float,
    # so it cannot ride in the boolean ``flags`` block; snapshotted as its own
    # key. Defaulted so pre-composite frozen snapshots still reconstruct.
    composite_coverage_min: float = 0.6

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable representation used for hashing and freezing."""
        return {
            "axis_weights": dict(self.axis_weights),
            "letter_bands": [list(band) for band in self.letter_bands],
            "nli_model": self.nli_model,
            "nli_params": dataclasses.asdict(self.nli_params),
            "abstention_thresholds": dict(self.abstention_thresholds),
            "flags": dict(self.flags),
            "int_flags": dict(self.int_flags),
            "composite_coverage_min": self.composite_coverage_min,
        }

    @staticmethod
    def capture_live() -> CampaignConfig:
        """Read the current process env + code constants into a config."""
        return CampaignConfig(
            axis_weights=dict(AXIS_WEIGHTS),
            letter_bands=tuple(LETTER_BANDS),
            nli_model=active_nli_model(),
            nli_params=load_nli_params(),
            abstention_thresholds=dict(ABSTENTION_THRESHOLDS),
            flags=snapshot_flags(),
            int_flags=snapshot_int_flags(),
            composite_coverage_min=apollo_composite_coverage_min(),
        )

    @staticmethod
    def from_snapshot(data: dict[str, Any]) -> CampaignConfig:
        """Reconstruct a :class:`CampaignConfig` from :meth:`snapshot` output.

        Backward-compatible with pre-``int_flags`` frozen snapshots (e.g. the
        f1/f1c/f2 config.json files): a missing key reconstructs as each
        tracked tunable's documented DEFAULT — which is what those runs
        actually graded under, since the env override did not exist yet.

        ``composite_coverage_min`` is read with a 0.6 fallback so frozen
        config.json files written BEFORE the composite gate existed (e.g.
        ``campaign/out/f1c/config.json``) still reconstruct.
        """
        return CampaignConfig(
            axis_weights=dict(data["axis_weights"]),
            letter_bands=tuple(tuple(band) for band in data["letter_bands"]),
            nli_model=data["nli_model"],
            nli_params=NLIParams(**data["nli_params"]),
            abstention_thresholds=dict(data["abstention_thresholds"]),
            flags=dict(data["flags"]),
            int_flags=dict(data.get("int_flags", _INT_FLAG_DEFAULTS)),
            composite_coverage_min=float(data.get("composite_coverage_min", 0.6)),
        )


def config_sha(snapshot: dict[str, Any]) -> str:
    """Deterministic sha256 hex digest of a config snapshot dict."""
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def freeze(config: CampaignConfig, path: Path | str) -> dict[str, Any]:
    """Write ``{"config_sha": ..., "config": config.snapshot()}`` to ``path``.

    Returns the frozen dict that was written. Creates parent directories.
    """
    snapshot = config.snapshot()
    sha = config_sha(snapshot)
    frozen = {"config_sha": sha, "config": snapshot}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(frozen, indent=2, sort_keys=True), encoding="utf-8")
    return frozen


def load_frozen(path: Path | str) -> tuple[CampaignConfig, str]:
    """Load a frozen config.json, verifying ``config_sha`` matches the payload.

    Raises ``ValueError`` if the file has been tampered with (the recomputed
    hash of ``config`` does not match the stored ``config_sha``).
    """
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8"))
    snapshot = data["config"]
    expected_sha = data["config_sha"]
    actual_sha = config_sha(snapshot)
    if actual_sha != expected_sha:
        raise ValueError(
            f"config_sha mismatch in {target}: file claims {expected_sha}, "
            f"recomputed {actual_sha} from its own config payload"
        )
    return CampaignConfig.from_snapshot(snapshot), expected_sha


class ConfigDivergedError(RuntimeError):
    """Raised when a gate-phase run's live environment no longer matches the
    config it was frozen against during the tune phase."""


def assert_live_matches_frozen(frozen: CampaignConfig) -> None:
    """Refuse to proceed if the CURRENT live env/code config differs from
    ``frozen``. Used to gate-keep the gate phase (WU C3 contract)."""
    live = CampaignConfig.capture_live()
    if live.snapshot() != frozen.snapshot():
        raise ConfigDivergedError(
            "Live campaign config diverges from the frozen tune-phase snapshot; "
            "refusing to run the gate phase against un-calibrated settings. "
            f"live={live.snapshot()!r} frozen={frozen.snapshot()!r}"
        )
