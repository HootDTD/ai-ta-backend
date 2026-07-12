"""Wave-1 live-LLM smoke — validates the DAG-3 restructured prompts against a
REAL model (the one thing stubbed tests cannot do), end-to-end through the new
defect-retry harness and the DAG-2 gate-9 lint.

Runs FOUR live scenarios (a handful of gpt-4o/-mini calls, cents of cost):
  1. derive_reference_graph on a real calc-2 problem + a textbook-style worked
     solution, real concept vocabulary → gold-format defects + gate-9 verdict.
  2. find_or_generate GENERATE branch on a Bernoulli-style calc question with
     retrieved context spans → harness defects/retries + symbol_table uptake.
  3. find_or_generate PROSE branch (augment_recall=True, MGMT-style) →
     must produce zero symbolic defects and a sane explain-why augmentation.
  4. derive_reference_graph on an MGMT-style prose derivation with an EMPTY
     concept vocabulary (the authored-set production path) → prose node types,
     no forced equations, zero defects, gates 4/6/7/9 self-deactivated.

NOT a pytest module (costs money, hits the network): run manually —
    python scripts/wave1_live_smoke.py
Reads OPENAI_API_KEY from the main checkout's .env (never modifies it).
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# --- environment: .env from the MAIN checkout + the local-CA workaround ----- #
_ENV_FILE = pathlib.Path("/Users/ishaanbatra/Documents/GitHub/ai-ta-backend/.env")
for line in _ENV_FILE.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

from apollo.provisioning.authored_sets.graph_derivation import (  # noqa: E402
    derive_reference_graph,
    find_derivation_defects,
)
from apollo.provisioning.metered_chat import MeteredChat  # noqa: E402
from apollo.provisioning.promotion_lint import (  # noqa: E402
    content_active_gates,
    run_promotion_lint,
)
from apollo.provisioning.solution import GroundingSpan, find_or_generate  # noqa: E402

CONCEPT_DIR = REPO / "apollo/subjects/calculus_2/concepts/integration_by_parts"

WORKED_SOLUTION = """
We evaluate the integral of x * e^x dx using integration by parts.
Recall the integration by parts formula: integral u dv = u*v - integral v du.
Choose u = x (it simplifies when differentiated) and dv = e^x dx.
Then du = dx and v = e^x.
Substituting into the formula: integral x e^x dx = x*e^x - integral e^x dx.
The remaining integral is elementary: integral e^x dx = e^x.
Therefore F = x*e^x - e^x + C, which factors as (x - 1)*e^x + C.
"""


def _fresh_meter() -> MeteredChat:
    from decimal import Decimal

    run = SimpleNamespace(llm_calls=0, llm_tokens_in=0, llm_tokens_out=0, llm_cost_usd=Decimal("0"))
    meter = MeteredChat(ingest_run=run)
    meter._smoke_run = run  # type: ignore[attr-defined]
    return meter


def _spans(texts: list[str], carries_solution: bool = False) -> list[GroundingSpan]:
    return [GroundingSpan(text=t, carries_solution=carries_solution) for t in texts]


def _report(title: str, lines: list[str]) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
    for line in lines:
        print(f"  {line}")


async def scenario_1_derivation() -> None:
    problem = json.loads((CONCEPT_DIR / "problems/problem_01.json").read_text())
    canonical_symbols = json.loads((CONCEPT_DIR / "canonical_symbols.json").read_text())
    normalization_map = json.loads((CONCEPT_DIR / "normalization_map.json").read_text())
    candidate = SimpleNamespace(
        problem_text=problem["problem_text"],
        given_values=problem.get("given_values", {}),
        target_unknown=problem.get("target_unknown", ""),
        difficulty=problem.get("difficulty", "standard"),
        chunk_content_hash="wave1smoke01",
    )
    meter = _fresh_meter()
    derived = await derive_reference_graph(
        candidate,
        _spans([WORKED_SOLUTION]),
        concept_slug="integration_by_parts",
        concept_display_name="Integration by Parts",
        canonical_symbols=canonical_symbols,
        normalization_map=normalization_map,
        chat_fn=meter.main,
    )
    graph = {
        "id": "derived.wave1smoke01",
        "concept_id": "integration_by_parts",
        "difficulty": candidate.difficulty,
        "problem_text": candidate.problem_text,
        "given_values": dict(candidate.given_values),
        "target_unknown": derived.target_unknown,
        "reference_solution": derived.reference_solution,
        "symbolic_mappings": derived.symbolic_mappings,
        "bound_variables": derived.bound_variables,
    }
    defects = find_derivation_defects(
        graph, canonical_symbols=canonical_symbols, normalization_map=normalization_map
    )
    lint = run_promotion_lint(
        graph,
        canonical_symbols=set(canonical_symbols["symbols"]),
        normalization_map=normalization_map,
        existing_problem_hashes=set(),
        active_gates=content_active_gates(graph),
    )
    run = meter._smoke_run  # type: ignore[attr-defined]
    _report(
        "1. DERIVATION (calc-2 integration by parts, live gpt)",
        [
            f"nodes={len(derived.reference_solution)} retried={derived.retried}",
            "ids: " + ", ".join(str(s.get("id")) for s in derived.reference_solution),
            f"target={derived.target_unknown!r} bound={derived.bound_variables}",
            f"post-hoc defects (all classes): {defects or 'NONE'}",
            f"gate lint: ok={lint.ok} failed_gate={lint.failed_gate} "
            f"verdict={getattr(lint, 'verdict', 'n/a')}",
            f"diagnostic: {lint.diagnostic[:160] or '(clean)'}",
            f"llm calls={run.llm_calls} tokens={run.llm_tokens_in}+{run.llm_tokens_out} "
            f"cost=${run.llm_cost_usd:.4f}",
        ],
    )


async def scenario_2_generate_calc() -> None:
    question = SimpleNamespace(
        problem_text=(
            "Water flows through a horizontal pipe that narrows from area A1 to "
            "area A2. The inlet speed is v1. Find the outlet speed v2."
        ),
        given_values={"A1": 0.02, "A2": 0.008, "v1": 3.0},
        target_unknown="v2",
        difficulty="standard",
        chunk_content_hash="wave1smoke02",
        concept_slug="continuity_equation",
        document_id=None,
        page=None,
    )
    context = [
        "For an incompressible fluid in steady flow, the continuity equation "
        "states that the volumetric flow rate is conserved: A1*v1 = A2*v2. "
        "When a pipe narrows, the fluid must speed up in proportion to the "
        "area ratio.",
        "Worked ideas: to find an unknown outlet speed, solve the continuity "
        "relation for v2, giving v2 = A1*v1/A2.",
    ]
    meter = _fresh_meter()

    async def retrieve_fn(_q):
        return _spans(context)

    draft = await find_or_generate(None, question, retrieve_fn=retrieve_fn, chat_fn=meter.main)
    run = meter._smoke_run  # type: ignore[attr-defined]
    _report(
        "2. FIND_OR_GENERATE calc generate-branch (live gpt)",
        [
            f"solution_source={draft.solution_source} steps={len(draft.reference_solution)}",
            "ids: " + ", ".join(str(s.get("id")) for s in draft.reference_solution),
            f"flagged defects: {draft.provenance.get('generation_defects', 'NONE')}",
            f"symbol_table emitted: {bool(draft.provenance.get('symbol_table'))} "
            f"{list((draft.provenance.get('symbol_table') or {}).keys())}",
            f"llm calls={run.llm_calls} (1 = no retries) tokens={run.llm_tokens_in}"
            f"+{run.llm_tokens_out} cost=${run.llm_cost_usd:.4f}",
        ],
    )


async def scenario_3_generate_prose() -> None:
    question = SimpleNamespace(
        problem_text="Define 'span of control' in organizational design.",
        given_values={},
        target_unknown="span of control",
        difficulty="intro",
        chunk_content_hash="wave1smoke03",
        concept_slug="org_structure",
        document_id=None,
        page=None,
    )
    context = [
        "Span of control is the number of subordinates a manager directly "
        "supervises. Wide spans flatten organizations and speed decisions but "
        "strain supervision quality; narrow spans deepen hierarchy, adding "
        "control at the cost of slower communication and higher overhead.",
    ]
    meter = _fresh_meter()

    async def retrieve_fn(_q):
        return _spans(context)

    draft = await find_or_generate(
        None, question, retrieve_fn=retrieve_fn, chat_fn=meter.main, augment_recall=True
    )
    run = meter._smoke_run  # type: ignore[attr-defined]
    _report(
        "3. FIND_OR_GENERATE prose augment-branch (live gpt)",
        [
            f"solution_source={draft.solution_source} steps={len(draft.reference_solution)}",
            "ids: " + ", ".join(str(s.get("id")) for s in draft.reference_solution),
            f"augmented_problem_text: {draft.augmented_problem_text!r}",
            f"flagged defects (MUST be NONE for prose): "
            f"{draft.provenance.get('generation_defects', 'NONE')}",
            f"llm calls={run.llm_calls} tokens={run.llm_tokens_in}+{run.llm_tokens_out} "
            f"cost=${run.llm_cost_usd:.4f}",
        ],
    )


MGMT_WORKED_SOLUTION = """
Model answer. Span of control is the number of subordinates who report directly
to a single manager. It matters because it fixes the shape of the hierarchy:
holding headcount constant, widening the span removes management layers and
flattens the organization, while narrowing it adds layers and deepens it.
A wide span lowers overhead cost and speeds vertical communication, but it
stretches the manager's attention, so it works only when work is standardized,
subordinates are experienced, and tasks are similar. A narrow span gives close
supervision and coaching, which suits novel or high-risk work, but it slows
decisions and encourages micromanagement. Therefore the appropriate span is a
contingency choice: match it to task routineness, subordinate skill, and the
cost of supervision errors, rather than treating one number as universally
correct.
"""


async def scenario_4_derivation_prose() -> None:
    """The MGMT production path: authored-set reversed provisioning derives a
    reference graph FROM a prose worked solution — no symbol vocabulary at all.
    Expect prose nodes (definitions/conditions/procedure_steps), NO forced
    equations, zero symbolic defects, meaningful ids, 5-9 nodes."""
    candidate = SimpleNamespace(
        problem_text=(
            "Explain what 'span of control' means in organizational design and "
            "why the appropriate span differs across organizations."
        ),
        given_values={},
        target_unknown="span of control",
        difficulty="intro",
        chunk_content_hash="wave1smoke04",
    )
    empty_vocab = {"symbols": [], "description": {}, "subscript_convention": ""}
    meter = _fresh_meter()
    derived = await derive_reference_graph(
        candidate,
        _spans([MGMT_WORKED_SOLUTION]),
        concept_slug="span_of_control",
        concept_display_name="Span of Control",
        canonical_symbols=empty_vocab,
        normalization_map={},
        chat_fn=meter.main,
    )
    graph = {
        "id": "derived.wave1smoke04",
        "concept_id": "span_of_control",
        "difficulty": candidate.difficulty,
        "problem_text": candidate.problem_text,
        "given_values": {},
        "target_unknown": derived.target_unknown,
        "reference_solution": derived.reference_solution,
        "symbolic_mappings": derived.symbolic_mappings,
        "bound_variables": derived.bound_variables,
    }
    defects = find_derivation_defects(graph, canonical_symbols=empty_vocab, normalization_map={})
    lint = run_promotion_lint(
        graph,
        canonical_symbols=set(),
        normalization_map={},
        existing_problem_hashes=set(),
        active_gates=content_active_gates(graph),
    )
    type_counts: dict[str, int] = {}
    for step in derived.reference_solution:
        type_counts[str(step.get("entry_type"))] = (
            type_counts.get(str(step.get("entry_type")), 0) + 1
        )
    run = meter._smoke_run  # type: ignore[attr-defined]
    _report(
        "4. DERIVATION on MGMT-style prose (live gpt — the authored-set path)",
        [
            f"nodes={len(derived.reference_solution)} retried={derived.retried}",
            f"entry types: {type_counts}  (equations here MUST be 0 or display-only)",
            "ids: " + ", ".join(str(s.get("id")) for s in derived.reference_solution),
            f"target={derived.target_unknown!r} bound={derived.bound_variables}",
            f"post-hoc defects (all classes): {defects or 'NONE'}",
            f"gate lint (pre-entity-key, gate 2 fail expected): ok={lint.ok} "
            f"failed_gate={lint.failed_gate} verdict={getattr(lint, 'verdict', 'n/a')}",
            f"active gates: {sorted(content_active_gates(graph))} "
            "(4/6/7/9 absent == symbolic layers correctly self-deactivated)",
            f"llm calls={run.llm_calls} tokens={run.llm_tokens_in}+{run.llm_tokens_out}",
        ],
    )


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not found (checked env + main checkout .env)")
    for scenario in (
        scenario_1_derivation,
        scenario_2_generate_calc,
        scenario_3_generate_prose,
        scenario_4_derivation_prose,
    ):
        try:
            await scenario()
        except Exception as exc:  # noqa: BLE001 — smoke must report, not die on #1
            _report(f"{scenario.__name__} FAILED", [repr(exc)[:500]])
    print("\nDone. Review each block: defects NONE (or sensible retries), lint ok,")
    print("prose zero-defect, meaningful ids, symbol_table uptake is a bonus signal.")


if __name__ == "__main__":
    asyncio.run(main())
