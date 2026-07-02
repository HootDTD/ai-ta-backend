"""Campaign run directory + run context (WU C3).

``RunContext.create`` sets up ``campaign/out/<run_id>/`` for one campaign run:

- ``config.json``   — the frozen :class:`~campaign.config.CampaignConfig` snapshot
- ``attempts.jsonl`` — append-only attempt log (created empty, never truncated
  on a re-create so a resumed run keeps its history)
- ``artifacts/``    — per-attempt artifact output directory

Two phases:

- ``"tune"``: captures the LIVE config and freezes it into ``config.json``
  (first run) or leaves an existing frozen config untouched (resume).
- ``"gate"``: requires a config already frozen by a prior tune run and
  refuses to proceed (:class:`~campaign.config.ConfigDivergedError`) if the
  live environment has drifted from that frozen snapshot — a gate run must
  execute against exactly the settings it was calibrated against.

On success, ``RunContext.create`` also exports the frozen ``config_sha`` into
the process environment (:data:`campaign.config.CONFIG_SHA_ENV_VAR`) so the
campaign driver (D3) can stamp it into every artifact's
``versions.weights_version`` without needing a reference back to this object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from campaign.config import (
    CONFIG_SHA_ENV_VAR,
    CampaignConfig,
    assert_live_matches_frozen,
    freeze,
    load_frozen,
)

Phase = Literal["tune", "gate"]
_VALID_PHASES: tuple[Phase, ...] = ("tune", "gate")

DEFAULT_OUT_ROOT = Path("campaign/out")


@dataclass(frozen=True)
class RunContext:
    """Scaffolding + frozen config identity for one campaign run."""

    run_id: str
    phase: Phase
    out_dir: Path
    config_path: Path
    attempts_path: Path
    artifacts_dir: Path
    config_sha: str

    @staticmethod
    def create(
        run_id: str,
        phase: str,
        *,
        out_root: Path | str = DEFAULT_OUT_ROOT,
        config: CampaignConfig | None = None,
    ) -> RunContext:
        """Create (or resume) the run directory for ``run_id``.

        ``config`` overrides the captured-live config for the tune phase
        (mainly for tests); it is ignored for the gate phase, which always
        loads whatever was frozen by the corresponding tune run.
        """
        if phase not in _VALID_PHASES:
            raise ValueError(f"phase must be 'tune' or 'gate', got {phase!r}")

        out_dir = Path(out_root) / run_id
        artifacts_dir = out_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        config_path = out_dir / "config.json"
        attempts_path = out_dir / "attempts.jsonl"

        if phase == "gate":
            if not config_path.exists():
                raise FileNotFoundError(
                    f"gate phase requires a config already frozen by a tune run "
                    f"at {config_path}; run the tune phase for {run_id!r} first"
                )
            frozen_config, sha = load_frozen(config_path)
            assert_live_matches_frozen(frozen_config)
        else:
            if config_path.exists():
                _, sha = load_frozen(config_path)
            else:
                live_config = config if config is not None else CampaignConfig.capture_live()
                frozen = freeze(live_config, config_path)
                sha = frozen["config_sha"]

        if not attempts_path.exists():
            attempts_path.touch()

        os.environ[CONFIG_SHA_ENV_VAR] = sha

        return RunContext(
            run_id=run_id,
            phase=phase,  # type: ignore[arg-type]
            out_dir=out_dir,
            config_path=config_path,
            attempts_path=attempts_path,
            artifacts_dir=artifacts_dir,
            config_sha=sha,
        )
