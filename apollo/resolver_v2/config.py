"""Resolver V2 flags + parameters (design §7, task T1).

Master switch is ``APOLLO_RESOLVER_V2`` — DEFAULT-OFF (flag-OFF must leave the
grading pipeline byte-identical, design invariant). Mirrors
``apollo.resolution.nli_config`` in shape (flag reader + frozen params
dataclass + ``load_params`` env overlay) but with the opposite default: this
engine is a shadow experiment, not production.

Every knob is read FRESH per call from the environment (no process-lived
caching) so tests' ``monkeypatch.setenv`` and calibration sweeps are honored
without a restart. Malformed env values fall back to the field default
(mirror ``apollo/grading/composite.py``'s env-float reader).

The ``ResolverV2Params`` field defaults carry T8's fitted values
(``campaign/out/resolver-v2/calibration-2026-07-07.json``): the F1c sweep's
entailment scores are bimodally saturated, so the grid collapses to one
operating point — the fit pins ``t_low=0.30``, ``t_high=0.90``, ``alpha=0.75``
(lexical fusion buys strong-vs-misconception separation at the full-credit
tier) and is indifferent on the rest, which therefore keep the design §7
defaults. The §9 FCR<=5% constraint was infeasible at every grid point —
per the committed ``negative_audit``, the gold per-node negatives are noisy
(personas teach most "omitted" content inline); see the calibration JSON.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

RESOLVER_V2_FLAG: str = "APOLLO_RESOLVER_V2"
GRAYZONE_FLAG: str = "APOLLO_RESOLVER_V2_GRAYZONE"
TRACE_DIR_ENV: str = "APOLLO_RESOLVER_V2_TRACE_DIR"

_TRUTHY: tuple[str, ...] = ("1", "true", "yes")


def _env_truthy(name: str) -> bool:
    """DEFAULT-OFF flag reader: unset -> False; truthy = ``1|true|yes``
    (case-insensitive, stripped). Mirrors ``nli_config.nli_enabled``'s
    truthiness set but inverts the unset default."""
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def resolver_v2_enabled() -> bool:
    """Whether the Resolver V2 shadow scoring engine runs at all (step 8b in
    ``done_grading.py``). DEFAULT-OFF: unset means the pipeline is
    byte-identical to v1."""
    return _env_truthy(RESOLVER_V2_FLAG)


def grayzone_enabled() -> bool:
    """Whether the grounded gray-zone LLM check (design §6) runs. DEFAULT-OFF
    — the deterministic replay/calibration mode where gray-band nodes stay at
    the 0.3 gray default."""
    return _env_truthy(GRAYZONE_FLAG)


def _env_float(name: str, default: float) -> float:
    """Read ``name`` from the environment as a float; fall back to ``default``
    on missing or malformed (mirrors ``apollo/grading/composite.py``)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """Read ``name`` from the environment as an int; fall back to ``default``
    on missing or malformed."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ResolverV2Params:
    """Every Resolver V2 tunable (design §5.1-§6 + the §7 env table).

    ``t_low/t_mid/t_high/alpha/max_contradiction`` are the T8 calibration
    surface; the rest are design-fixed defaults, still env-overridable for
    replay levers (§8: ``TOP_K=2``, ``MAX_PAIRS=120``)."""

    # §5.5 credit thresholds (T8 fitted, calibration-2026-07-07.json)
    t_low: float = 0.30
    t_mid: float = 0.70
    t_high: float = 0.90
    # §5.4 fusion weight on entailment + pair contradiction veto (T8 fitted)
    alpha: float = 0.75
    max_contradiction: float = 0.30
    # §5.3 prefilter: windows per (node, view) sent to NLI + node skip floor
    top_k_windows: int = 3
    lex_floor: float = 0.10
    # §5.4 per-attempt NLI pair budget
    max_nli_pairs: int = 200
    # §5.6 edges: direct edge-entailment bar + endpoint pull-up floor
    t_edge: float = 0.85
    edge_pullup_floor: float = 0.6
    # §5.1 windowing
    window_sentences: int = 3
    window_overlap_sentences: int = 1
    max_window_words: int = 120
    # §6 gray zone
    max_grayzone_nodes: int = 8
    grayzone_credit: float = 0.7
    # Symbolic-blindness gate (T8 anomaly #2, REPORT.md §10.3): max credit an
    # EQUATION-type node may earn from text evidence alone (NLI/lexical) —
    # entailment is blind to symbolic coefficients (it scored
    # ``x = v0*t + a*t^2`` at 0.98 against the half-factor reference).
    # Default = the gray-band credit (scoring._CREDIT_GRAY), so a paraphrase
    # can at most make an equation node gray-status (grayzone/clarification
    # eligible); full credit requires the v1 symbolic floor. Nodes upgraded
    # via grayzone still carry grayzone's 0.7 cap — a verified quote is text
    # too, never full equation credit. Calibration owns this value.
    equation_text_credit_cap: float = 0.3
    # §7 trace dump target (None = no dump; integration writes
    # <trace_dir>/attempt_<id>.json, new files only)
    trace_dir: str | None = None


# Single source of the construction defaults for load_params, so T8's fitted
# values only ever land in the class fields above.
_DEFAULTS: ResolverV2Params = ResolverV2Params()


def load_params() -> ResolverV2Params:
    """Build :class:`ResolverV2Params` from the environment: class defaults
    with any ``APOLLO_RESOLVER_V2_<FIELD>`` override applied on top (§7 table
    names are authoritative — note ``TOP_K`` -> ``top_k_windows`` and
    ``MAX_PAIRS`` -> ``max_nli_pairs``). Missing/malformed -> default."""
    return ResolverV2Params(
        t_low=_env_float("APOLLO_RESOLVER_V2_T_LOW", _DEFAULTS.t_low),
        t_mid=_env_float("APOLLO_RESOLVER_V2_T_MID", _DEFAULTS.t_mid),
        t_high=_env_float("APOLLO_RESOLVER_V2_T_HIGH", _DEFAULTS.t_high),
        alpha=_env_float("APOLLO_RESOLVER_V2_ALPHA", _DEFAULTS.alpha),
        max_contradiction=_env_float(
            "APOLLO_RESOLVER_V2_MAX_CONTRADICTION", _DEFAULTS.max_contradiction
        ),
        top_k_windows=_env_int("APOLLO_RESOLVER_V2_TOP_K", _DEFAULTS.top_k_windows),
        lex_floor=_env_float("APOLLO_RESOLVER_V2_LEX_FLOOR", _DEFAULTS.lex_floor),
        max_nli_pairs=_env_int("APOLLO_RESOLVER_V2_MAX_PAIRS", _DEFAULTS.max_nli_pairs),
        t_edge=_env_float("APOLLO_RESOLVER_V2_T_EDGE", _DEFAULTS.t_edge),
        edge_pullup_floor=_env_float(
            "APOLLO_RESOLVER_V2_EDGE_PULLUP_FLOOR", _DEFAULTS.edge_pullup_floor
        ),
        window_sentences=_env_int(
            "APOLLO_RESOLVER_V2_WINDOW_SENTENCES", _DEFAULTS.window_sentences
        ),
        window_overlap_sentences=_env_int(
            "APOLLO_RESOLVER_V2_WINDOW_OVERLAP_SENTENCES",
            _DEFAULTS.window_overlap_sentences,
        ),
        max_window_words=_env_int(
            "APOLLO_RESOLVER_V2_MAX_WINDOW_WORDS", _DEFAULTS.max_window_words
        ),
        max_grayzone_nodes=_env_int(
            "APOLLO_RESOLVER_V2_MAX_GRAYZONE_NODES", _DEFAULTS.max_grayzone_nodes
        ),
        grayzone_credit=_env_float(
            "APOLLO_RESOLVER_V2_GRAYZONE_CREDIT", _DEFAULTS.grayzone_credit
        ),
        equation_text_credit_cap=_env_float(
            "APOLLO_RESOLVER_V2_EQUATION_TEXT_CREDIT_CAP",
            _DEFAULTS.equation_text_credit_cap,
        ),
        trace_dir=os.environ.get(TRACE_DIR_ENV) or None,
    )
