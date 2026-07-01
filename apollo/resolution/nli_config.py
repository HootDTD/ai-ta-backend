"""NLI configuration and parameters for Apollo resolution tier.

Thresholds are tuned PER MODEL (small vs large NLI checkpoints behave
differently). ``load_nli_params`` resolves the tuned defaults for the *active*
checkpoint and then applies any ``APOLLO_NLI_*`` env overrides on top. The
frozen ``NLIParams`` field defaults below are NEUTRAL construction defaults
(used by ``calibration.best_operating_point`` and tests) — the production floors
come from the per-model registry, NOT from the class-level ``0.80`` veto.

Calibration source: ``docs/_archive/experiments/2026-07-01-nli-threshold-tuning``
(precision-first: precision 1.000 with zero false credits on the dev set,
end-to-end gate PASS for both checkpoints).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

NLI_ENABLED_FLAG: str = "APOLLO_NLI_ENABLED"
NLI_MODEL_ENV: str = "APOLLO_NLI_MODEL"
NLI_DEVICE: str = "cpu"

NLI_MODEL_SMALL: str = "cross-encoder/nli-deberta-v3-small"
NLI_MODEL_LARGE: str = "cross-encoder/nli-deberta-v3-large"

#: The production NLI checkpoint (behind ``APOLLO_NLI_ENABLED``). The ``-large``
#: model is the tuned target — higher recall (0.714 vs 0.661) at a far more
#: robust threshold, and it recognises domain paraphrases the small model misses
#: (2026-07-01 tuning). Kept as a module constant for back-compat imports;
#: runtime code should call :func:`active_nli_model` so ``APOLLO_NLI_MODEL`` wins.
NLI_MODEL_NAME: str = NLI_MODEL_LARGE


def nli_enabled() -> bool:
    """Whether the NLI resolver tier is active.

    DEFAULT-ON (2026-07-01, after the per-model threshold tuning cleared the
    precision gate): an UNSET ``APOLLO_NLI_ENABLED`` means enabled. A deployment
    (or the test-suite guards) forces it OFF with ``APOLLO_NLI_ENABLED=0`` (or any
    non-truthy value) — the kill switch."""
    raw = os.environ.get(NLI_ENABLED_FLAG)
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes")


def active_nli_model() -> str:
    """The NLI checkpoint to load, env-overridable via ``APOLLO_NLI_MODEL``.
    Defaults to the tuned production target (:data:`NLI_MODEL_LARGE`)."""
    return os.environ.get(NLI_MODEL_ENV, NLI_MODEL_LARGE)


@dataclass(frozen=True)
class NLIParams:
    """NLI parameters (frozen dataclass).

    These field defaults are NEUTRAL construction defaults, not the production
    floors. Production params come from :func:`load_nli_params` →
    :func:`default_params_for_model`; the ``0.80`` veto here would be unsafe as a
    production floor (a correct student statement can false-entail a
    misconception surface at 0.88-0.98 — 2026-07-01 tuning §3)."""

    top_k: int = 5
    min_entailment: float = 0.87
    max_contradiction: float = 0.10
    ambiguity_margin: float = 0.10  # top-2 entailment separation (NOT a per-candidate gate)
    misconception_veto_entailment: float = 0.80


# Per-model tuned defaults (2026-07-01 threshold tuning). Precision-first: each
# clears the ≥0.95 precision floor with zero false credits on the dev set and
# passes the end-to-end resolve_attempt gate.
_PER_MODEL_DEFAULTS: dict[str, NLIParams] = {
    NLI_MODEL_LARGE: NLIParams(
        min_entailment=0.90, max_contradiction=0.05, misconception_veto_entailment=0.98
    ),
    NLI_MODEL_SMALL: NLIParams(
        min_entailment=0.70, max_contradiction=0.10, misconception_veto_entailment=0.96
    ),
}

# Fallback for an unrecognised checkpoint: conservative recall bar + a SAFE veto
# floor. Never the class-default 0.80 (that false-vetoes correct students).
_FALLBACK_DEFAULTS: NLIParams = NLIParams(
    min_entailment=0.90, max_contradiction=0.05, misconception_veto_entailment=0.98
)


def default_params_for_model(model_name: str) -> NLIParams:
    """The tuned default :class:`NLIParams` for a checkpoint; a safe fallback
    (conservative min_entailment, veto ≥ 0.96) for any unrecognised model."""
    return _PER_MODEL_DEFAULTS.get(model_name, _FALLBACK_DEFAULTS)


def _env_float(name: str, default: float) -> float:
    """Parse float from environment variable or return default."""
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _env_int(name: str, default: int) -> int:
    """Parse int from environment variable or return default."""
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


def load_nli_params() -> NLIParams:
    """Load NLI parameters: the tuned defaults for the ACTIVE checkpoint
    (:func:`active_nli_model`), with any ``APOLLO_NLI_*`` env override applied on
    top of the per-model base."""
    base = default_params_for_model(active_nli_model())
    return NLIParams(
        top_k=_env_int("APOLLO_NLI_TOP_K", base.top_k),
        min_entailment=_env_float("APOLLO_NLI_MIN_ENTAILMENT", base.min_entailment),
        max_contradiction=_env_float("APOLLO_NLI_MAX_CONTRADICTION", base.max_contradiction),
        ambiguity_margin=_env_float("APOLLO_NLI_AMBIGUITY_MARGIN", base.ambiguity_margin),
        misconception_veto_entailment=_env_float(
            "APOLLO_NLI_MISC_VETO_ENT", base.misconception_veto_entailment
        ),
    )
