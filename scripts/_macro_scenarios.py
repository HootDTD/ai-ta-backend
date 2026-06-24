"""Hand-authored teaching transcripts for the Macro Ch.6 graph-grading probe.

Three variations per problem — ``strong`` / ``partial`` / ``weak`` — faithful to
OpenStax Macro Ch.6 prose and each problem's authored ``reference_solution``:

* ``strong``  — complete, correct derivation that states every reference
  equation/condition/definition and walks the procedure.
* ``partial`` — correct but omits exactly one reference node/edge (a condition,
  a definition, or the procedure framing).
* ``weak``    — voices THIS concept's misconception (counts transfers / forgets
  depreciation / uses nominal for real / deflates the wrong direction) so it
  should resolve to a ``misc.*`` and drop soundness.

Each variation is a list of student ``/chat`` messages (one per teaching turn).
``MACRO_TRANSCRIPTS`` carries the per-problem Hoot intro transcript that drives
``infer_concept_id`` toward the right macro concept (so ``/sessions/from_hoot``
serves the intended problem). This module is pure data + lookup helpers — no DB,
no network, no I/O — so it is unit-testable on its own.

The bernoulli scenario from the original probe stays in ``apollo_grade_probe``;
this module is keyed only by the five macro problem ids.
"""

from __future__ import annotations

# Ordered list of the macro problem ids, in the order the probe should iterate.
MACRO_PROBLEM_IDS: tuple[str, ...] = (
    "gdp_identity",
    "net_exports_sign",
    "nnp_chain",
    "real_gdp_from_deflator",
    "real_gdp_growth",
)

# The variations every macro scenario provides (strong/partial/weak), in order.
MACRO_VARIATIONS: tuple[str, ...] = ("strong", "partial", "weak")

# Per-problem Hoot intro transcript — steers infer_concept_id toward the concept
# that owns the problem so /sessions/from_hoot serves the intended one.
MACRO_TRANSCRIPTS: dict[str, str] = {
    "gdp_identity": (
        "I want to understand how GDP is measured with the expenditure approach: "
        "adding up consumption, investment, government purchases, and net exports, "
        "and why only final goods and services are counted."
    ),
    "net_exports_sign": (
        "I want to understand net exports — exports minus imports — and how the "
        "sign of net exports tells you whether a country runs a trade surplus or a "
        "trade deficit."
    ),
    "nnp_chain": (
        "I want to understand how gross domestic product relates to gross national "
        "product and net national product, and how depreciation is subtracted to "
        "go from gross to net measures of output."
    ),
    "real_gdp_from_deflator": (
        "I want to understand the GDP deflator and how to use a price index to "
        "convert nominal GDP into real GDP in base-year dollars."
    ),
    "real_gdp_growth": (
        "I want to understand how to compute the percentage growth in real GDP "
        "between two years using inflation-adjusted figures."
    ),
}


# Reference graphs the transcripts are authored against (for reviewers):
#   Q1 gdp_identity      : eq.net_exports, eq.gdp_expenditure, cond.final_goods_only,
#                          proc.compute_net_exports, proc.sum_components
#   Q2 net_exports_sign  : eq.net_exports, cond.trade_deficit, proc.subtract_imports
#   Q3 nnp_chain         : eq.gnp, eq.nnp, def.depreciation, proc.compute_gnp,
#                          proc.subtract_depreciation
#   Q4 real_gdp_from_deflator : eq.gdp_deflator, simp.deflator_is_price_index,
#                               proc.rearrange_for_real_gdp  (the case-3 trap)
#   Q5 real_gdp_growth   : eq.growth_rate, def.real_basis, proc.compute_real_change,
#                          proc.apply_percent_change
MACRO_SCENARIOS: dict[str, dict[str, list[str]]] = {
    # --- Q1: GDP expenditure identity (states the condition; weak = transfers) --
    "gdp_identity": {
        "strong": [
            "GDP by the expenditure approach is the sum of four components: "
            "consumption, investment, government purchases, and net exports, so "
            "GDP = C + INV + G + NX.",
            "Net exports are exports minus imports: NX = X - M. Here X = 100 and "
            "M = 120, so NX = -20.",
            "Only final goods and services produced this year are counted in GDP; "
            "transfer payments, used goods, and intermediate goods are excluded.",
            "First I compute net exports NX = X - M = 100 - 120 = -20, then I add "
            "the components: GDP = C + INV + G + NX = 400 + 60 + 120 + (-20) = 560.",
        ],
        "partial": [
            "GDP by the expenditure approach is GDP = C + INV + G + NX, the sum of "
            "consumption, investment, government purchases, and net exports.",
            "Net exports are exports minus imports, NX = X - M = 100 - 120 = -20.",
            "So I add the components together: GDP = 400 + 60 + 120 + (-20) = 560.",
        ],
        "weak": [
            "To get GDP I add up all the spending in the economy, and that "
            "includes transfer payments like social security and welfare, plus "
            "sales of used cars, because that money is all spent.",
            "So I count consumption, investment, government purchases, the "
            "transfer payments, and the used-goods sales toward GDP.",
        ],
    },
    # --- Q2: net exports sign / trade deficit (polarity; weak = transfers) ------
    "net_exports_sign": {
        "strong": [
            "Net exports are defined as exports minus imports: NX = X - M.",
            "Because imports M = 120 exceed exports X = 100, net exports are "
            "negative, so the economy runs a trade deficit rather than a trade "
            "surplus.",
            "I subtract imports from exports: NX = X - M = 100 - 120 = -20, and "
            "since the result is negative it confirms a trade deficit.",
        ],
        "partial": [
            "Net exports are exports minus imports: NX = X - M.",
            "Subtracting, NX = 100 - 120 = -20.",
        ],
        "weak": [
            "Net exports should include the transfer payments the government sends "
            "abroad and the used goods we resell to other countries, so I add "
            "those to exports before subtracting imports.",
            "Counting those transfers in, the trade balance comes out positive, so "
            "I'd call it a surplus.",
        ],
    },
    # --- Q3: GNP -> NNP chain (multi-equation; weak = forgets depreciation) -----
    "nnp_chain": {
        "strong": [
            "Gross national product adds net income from abroad to GDP: "
            "GNP = GDP + RIN - ROUT, where RIN is income earned from abroad and "
            "ROUT is income paid to foreigners.",
            "Net national product subtracts depreciation from GNP: NNP = GNP - DEP.",
            "Depreciation is the value of capital worn out or used up during the "
            "year; it is subtracted from gross national product to get net "
            "national product, which is why net measures are smaller than gross.",
            "First I compute GNP = GDP + RIN - ROUT = 560 + 10 - 8 = 562.",
            "Then I subtract depreciation: NNP = GNP - DEP = 562 - 40 = 522.",
        ],
        "partial": [
            "Gross national product is GNP = GDP + RIN - ROUT = 560 + 10 - 8 = 562.",
            "Net national product subtracts depreciation: NNP = GNP - DEP.",
            "First I compute GNP = 562, then NNP = GNP - DEP = 562 - 40 = 522.",
        ],
        "weak": [
            "Gross national product is GNP = GDP + RIN - ROUT = 560 + 10 - 8 = 562.",
            "Net national product is basically the same as gross national product; "
            "there's no need to subtract depreciation, so NNP equals GNP = 562.",
        ],
    },
    # --- Q4: real GDP from the deflator — THE CASE-3 TRAP ----------------------
    # strong MUST: state the base deflator relation, assert PI is the deflator,
    # REARRANGE to realGDP = nomGDP/(PI/100), and compute. We observe whether the
    # rearranged USES edge attaches to eq.gdp_deflator (the {PI: deflator} fix).
    "real_gdp_from_deflator": {
        "strong": [
            "The GDP deflator is defined by deflator = (nomGDP/realGDP)*100 — it is "
            "the price index that relates nominal GDP to real GDP with the base "
            "year set to 100.",
            "The price index quoted for the year IS the GDP deflator, so I treat "
            "the given PI = 19.0 as the deflator in that definition.",
            "I rearrange the deflator definition to solve for real GDP: starting "
            "from deflator = (nomGDP/realGDP)*100, real GDP = nomGDP/(PI/100).",
            "Substituting nomGDP = 543.3 and PI = 19.0: "
            "realGDP = 543.3/(19.0/100) = 543.3/0.19 = 2859.5.",
        ],
        "partial": [
            "The GDP deflator is defined by deflator = (nomGDP/realGDP)*100, the "
            "price index relating nominal to real GDP with the base year at 100.",
            "Rearranging for real GDP, realGDP = nomGDP/(deflator/100) = "
            "543.3/(19.0/100) = 2859.5.",
        ],
        "weak": [
            "To convert nominal GDP to real GDP I multiply nominal GDP by the "
            "price index over 100, so real GDP = nomGDP * (PI/100).",
            "That gives realGDP = 543.3 * (19.0/100) = 543.3 * 0.19 = 103.2.",
        ],
    },
    # --- Q5: real GDP growth (states the real-basis definition; weak = nominal) -
    "real_gdp_growth": {
        "strong": [
            "The percentage growth rate is growth = ((g2 - g1)/g1)*100, the change "
            "from the earlier value g1 to the later value g2 over g1, times 100.",
            "Both figures are real GDP, which is inflation-adjusted (valued in "
            "constant base-year prices), so the change reflects a change in the "
            "quantity of output, not a change in the price level.",
            "First I take the change in real GDP, g2 - g1 = 13598.5 - 2859.5 = "
            "10739.0.",
            "Then I divide by g1 and multiply by 100: growth = "
            "(10739.0/2859.5)*100 ≈ 376 percent.",
        ],
        "partial": [
            "The percentage growth rate is growth = ((g2 - g1)/g1)*100.",
            "First the change is g2 - g1 = 13598.5 - 2859.5 = 10739.0.",
            "Then growth = (10739.0/2859.5)*100 ≈ 376 percent.",
        ],
        "weak": [
            "I'll just use nominal GDP for both years — nominal and real GDP are "
            "basically the same, so there's no need to adjust for inflation.",
            "Using the nominal figures directly, the growth is "
            "((13598.5 - 2859.5)/2859.5)*100 ≈ 376 percent, and that's the real "
            "growth too since I didn't need to deflate anything.",
        ],
    },
}


def macro_variation_messages(problem_id: str, variation: str) -> list[str] | None:
    """Return the authored ``/chat`` messages for one (problem, variation).

    Returns ``None`` when ``problem_id`` is not a macro scenario (so the caller
    can fall back to the generic transcript). Raises ``KeyError`` when the
    problem exists but the variation name is unknown — a programming error, not a
    served-a-different-problem fallback.
    """
    scenario = MACRO_SCENARIOS.get(problem_id)
    if scenario is None:
        return None
    return scenario[variation]


def macro_transcript(problem_id: str) -> str | None:
    """Hoot intro transcript that steers ``from_hoot`` to ``problem_id``.

    ``None`` when the problem id is not a known macro scenario.
    """
    return MACRO_TRANSCRIPTS.get(problem_id)
