"""NLI configuration and parameters for Apollo resolution tier."""

from __future__ import annotations

import os
from dataclasses import dataclass

NLI_ENABLED_FLAG: str = "APOLLO_NLI_ENABLED"
NLI_MODEL_NAME: str = "cross-encoder/nli-deberta-v3-small"
NLI_DEVICE: str = "cpu"


def nli_enabled() -> bool:
    """Check if NLI is enabled via environment flag."""
    return os.environ.get(NLI_ENABLED_FLAG, "").lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class NLIParams:
    """NLI parameters (frozen dataclass)."""

    top_k: int = 5
    min_entailment: float = 0.87
    max_contradiction: float = 0.10
    ambiguity_margin: float = 0.10  # top-2 entailment separation (NOT a per-candidate gate)
    misconception_veto_entailment: float = 0.80


def _env_float(name: str, default: float) -> float:
    """Parse float from environment variable or return default."""
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _env_int(name: str, default: int) -> int:
    """Parse int from environment variable or return default."""
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


def load_nli_params() -> NLIParams:
    """Load NLI parameters from environment or defaults."""
    return NLIParams(
        top_k=_env_int("APOLLO_NLI_TOP_K", 5),
        min_entailment=_env_float("APOLLO_NLI_MIN_ENTAILMENT", 0.87),
        max_contradiction=_env_float("APOLLO_NLI_MAX_CONTRADICTION", 0.10),
        ambiguity_margin=_env_float("APOLLO_NLI_AMBIGUITY_MARGIN", 0.10),
        misconception_veto_entailment=_env_float("APOLLO_NLI_MISC_VETO_ENT", 0.80),
    )
