"""WU-3B2f — auto-provisioning cost/attempt constants (pure config).

Three concerns, all env-overridable with committed defaults pinned by tests
(mirrors ``dedup_constants.py`` — no logic beyond arithmetic, no imports beyond
``os``/``decimal``):

  * ``PER_DOCUMENT_TOKEN_CEILING`` — a runaway CIRCUIT BREAKER (NOT a tight
    budget). The real cost control is ``APOLLO_AUTOPROVISION_ENABLED`` defaulting
    OFF everywhere; this is the per-document safety bound (ADJ #7). The call that
    pushes the cumulative (in+out) token total over the line aborts the run via
    ``metered_chat.CostBudgetExceeded``.
  * ``MAX_ATTEMPTS`` — the dead-letter cap. ``queue.fail_job`` moves a job to the
    terminal ``failed`` state once ``attempt_count >= MAX_ATTEMPTS``.
  * ``MODEL_PRICES`` + ``cost_usd_for`` — the model->price table (USD per 1M
    tokens, ADJ #7) and a pure Decimal cost helper. ``Decimal`` is load-bearing:
    ``apollo_ingest_runs.llm_cost_usd`` is ``NUMERIC(12,6)`` and float rounding
    would drift the exact cost-math tests.
"""

from __future__ import annotations

import os
from decimal import Decimal

# Runaway circuit-breaker: cumulative (in+out) tokens per document. The default
# is generous — a large chapter scrapes well below it; the breach is the abort.
PER_DOCUMENT_TOKEN_CEILING: int = int(
    os.getenv("APOLLO_PROVISION_TOKEN_CEILING", "2000000")
)

# Dead-letter cap: a job whose ``attempt_count`` reaches this is failed terminally
# by ``queue.fail_job`` (no further claim possible).
MAX_ATTEMPTS: int = int(os.getenv("APOLLO_PROVISION_MAX_ATTEMPTS", "3"))

# A million tokens — the denominator for the per-1M price table.
_TOKENS_PER_PRICE_UNIT: Decimal = Decimal("1000000")

# model -> (usd_per_1M_input, usd_per_1M_output). Keys are the resolved model
# strings the metered client routes to (the repo defaults; ADJ #7 pricing).
MODEL_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
}


def cost_usd_for(model: str, *, tokens_in: int, tokens_out: int) -> Decimal:
    """Decimal USD for one call.

    An unknown model returns ``Decimal('0')`` and NEVER raises — token counts
    still accrue on the ingest_run row; only the dollar estimate is best-effort.
    """
    prices = MODEL_PRICES.get(model)
    if prices is None:
        return Decimal("0")
    price_in, price_out = prices
    cost_in = (Decimal(tokens_in) / _TOKENS_PER_PRICE_UNIT) * price_in
    cost_out = (Decimal(tokens_out) / _TOKENS_PER_PRICE_UNIT) * price_out
    return cost_in + cost_out
