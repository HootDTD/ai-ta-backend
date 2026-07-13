"""DAG-4 live-LLM evaluation — compares legacy and KC-grained derivation.

Runs six inline fixtures (three calculus, three qualitative management) through
``derive_reference_graph`` with ``APOLLO_KC_GRANULARITY`` first OFF and then ON.
Reports node counts, defect/retry behavior, and content-selected promotion lint.

NOT a pytest module (costs money, hits the network): run manually —
    python scripts/dag4_granularity_eval.py
Reads OPENAI_API_KEY from the main checkout's .env (never modifies it).
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

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
from apollo.provisioning.solution import GroundingSpan  # noqa: E402

CONCEPT_DIR = REPO / "apollo/subjects/calculus_2/concepts/integration_by_parts"
KC_FLAG = "APOLLO_KC_GRANULARITY"

CALC_VOCAB = json.loads((CONCEPT_DIR / "canonical_symbols.json").read_text())
CALC_NORMALIZATION = json.loads((CONCEPT_DIR / "normalization_map.json").read_text())
EMPTY_VOCAB = {"symbols": [], "description": {}, "subscript_convention": ""}


@dataclass(frozen=True)
class Fixture:
    name: str
    corpus: str
    problem_text: str
    worked_solution: str
    target_unknown: str
    difficulty: str = "standard"


@dataclass(frozen=True)
class EvalResult:
    nodes: int | None
    defects: tuple[str, ...]
    retries: int
    lint: str
    active_gates: tuple[int, ...]
    ids: tuple[str, ...]
    error: str = ""


FIXTURES = (
    Fixture(
        name="calc_x_exp_x",
        corpus="calc",
        problem_text="Evaluate integral x*e^x dx.",
        target_unknown="F",
        difficulty="intro",
        worked_solution="""
Use integration by parts: integral u dv = u*v - integral v du. Choose u = x
and dv = e^x dx because differentiating x simplifies it. Then du = dx and
v = e^x. Substitute to obtain integral x*e^x dx = x*e^x - integral e^x dx.
Evaluate the remaining integral and add the constant: F = x*e^x - e^x + C.
""",
    ),
    Fixture(
        name="calc_x_cos_x",
        corpus="calc",
        problem_text="Evaluate integral x*cos(x) dx.",
        target_unknown="F",
        difficulty="intro",
        worked_solution="""
Apply integration by parts with u = x and dv = cos(x) dx. This choice makes
du = dx and v = sin(x). Using integral u dv = u*v - integral v du gives
integral x*cos(x) dx = x*sin(x) - integral sin(x) dx. Since the remaining
integral is -cos(x), the antiderivative is F = x*sin(x) + cos(x) + C.
""",
    ),
    Fixture(
        name="calc_ln_x",
        corpus="calc",
        problem_text="Evaluate integral ln(x) dx.",
        target_unknown="F",
        worked_solution="""
Treat ln(x) as ln(x)*1 and use integration by parts. Choose u = ln(x) and
dv = dx, so du = (1/x) dx and v = x. The parts formula yields
integral ln(x) dx = x*ln(x) - integral x*(1/x) dx. Simplify the remaining
integrand to 1 and integrate it, giving F = x*ln(x) - x + C.
""",
    ),
    Fixture(
        name="mgmt_span_of_control",
        corpus="mgmt",
        problem_text=(
            "Explain span of control and why the appropriate span differs across organizations."
        ),
        target_unknown="span of control",
        difficulty="intro",
        worked_solution="""
Span of control is the number of subordinates who report directly to one
manager. Widening the span usually removes management layers, reduces overhead,
and speeds vertical communication, but stretches the manager's attention.
Narrowing it supports close supervision and coaching, but adds hierarchy and
can slow decisions. The appropriate span is therefore contingent on task
routineness, employee experience, coordination needs, and the cost of errors.
""",
    ),
    Fixture(
        name="mgmt_centralization",
        corpus="mgmt",
        problem_text=(
            "Compare centralized and decentralized decision authority and "
            "explain when each is useful."
        ),
        target_unknown="decision authority design",
        worked_solution="""
Centralization concentrates important decisions near the top of the hierarchy.
It supports consistency, scale economies, and tight control, so it is useful
when risk is high or a uniform response matters. Decentralization delegates
authority closer to operating information. It improves local responsiveness
and develops managers, but can duplicate effort or produce inconsistent
choices. The design should match environmental uncertainty, local knowledge,
coordination costs, and the organization's need for standardization.
""",
    ),
    Fixture(
        name="mgmt_psychological_safety",
        corpus="mgmt",
        problem_text=(
            "Explain how psychological safety can improve team learning "
            "without eliminating accountability."
        ),
        target_unknown="psychological safety",
        worked_solution="""
Psychological safety is a shared belief that interpersonal risk-taking, such
as asking questions or admitting mistakes, will not trigger humiliation or
punishment. It improves team learning because members surface uncertainty,
errors, and dissent early enough for the group to respond. It is not the same
as low standards: accountability specifies demanding performance expectations,
while safety makes it possible to discuss gaps honestly. Leaders combine both
by inviting input, responding constructively, and following through on clear
commitments and consequences.
""",
    ),
)


def _fresh_meter() -> MeteredChat:
    from decimal import Decimal

    run = SimpleNamespace(
        llm_calls=0,
        llm_tokens_in=0,
        llm_tokens_out=0,
        llm_cost_usd=Decimal("0"),
    )
    meter = MeteredChat(ingest_run=run)
    meter._dag4_run = run  # type: ignore[attr-defined]
    return meter


def _graph(fixture: Fixture, parsed: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"derived.dag4.{fixture.name}",
        "concept_id": ("integration_by_parts" if fixture.corpus == "calc" else fixture.name),
        "difficulty": fixture.difficulty,
        "problem_text": fixture.problem_text,
        "given_values": {},
        "target_unknown": str(parsed.get("target_unknown") or ""),
        "reference_solution": list(parsed.get("reference_solution") or []),
        "symbolic_mappings": dict(parsed.get("symbolic_mappings") or {}),
        "bound_variables": [str(v) for v in (parsed.get("bound_variables") or [])],
    }


def _attempt_defects(
    fixture: Fixture,
    raw: str,
    *,
    canonical_symbols: dict,
    normalization_map: dict,
) -> set[str]:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"unparseable"}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("reference_solution"), list):
        return {"unparseable"}
    defects = find_derivation_defects(
        _graph(fixture, parsed),
        canonical_symbols=canonical_symbols,
        normalization_map=normalization_map,
    )
    return {defect.partition(":")[0] for defect in defects}


async def _run_mode(fixture: Fixture, *, enabled: bool) -> EvalResult:
    canonical_symbols = CALC_VOCAB if fixture.corpus == "calc" else EMPTY_VOCAB
    normalization_map = CALC_NORMALIZATION if fixture.corpus == "calc" else {}
    meter = _fresh_meter()
    raw_responses: list[str] = []

    def recording_chat(**kwargs) -> str:
        raw = meter.main(**kwargs)
        raw_responses.append(raw)
        return raw

    previous = os.environ.get(KC_FLAG)
    if enabled:
        os.environ[KC_FLAG] = "1"
    else:
        os.environ.pop(KC_FLAG, None)
    try:
        candidate = SimpleNamespace(
            problem_text=fixture.problem_text,
            given_values={},
            target_unknown=fixture.target_unknown,
            difficulty=fixture.difficulty,
            chunk_content_hash=f"dag4-{fixture.name}",
        )
        derived = await derive_reference_graph(
            candidate,
            [GroundingSpan(text=fixture.worked_solution, carries_solution=True)],
            concept_slug=("integration_by_parts" if fixture.corpus == "calc" else fixture.name),
            concept_display_name=fixture.name.replace("_", " ").title(),
            canonical_symbols=canonical_symbols,
            normalization_map=normalization_map,
            chat_fn=recording_chat,
        )
        graph = _graph(fixture, derived.model_dump())
        active_gates = content_active_gates(graph)
        lint = run_promotion_lint(
            graph,
            canonical_symbols=set(canonical_symbols["symbols"]),
            normalization_map=normalization_map,
            existing_problem_hashes=set(),
            active_gates=active_gates,
        )
        lint_label = (
            f"PASS/{getattr(lint, 'verdict', 'n/a')}"
            if lint.ok
            else f"FAIL-g{lint.failed_gate}/{getattr(lint, 'verdict', 'n/a')}"
        )
        error = ""
        nodes = len(derived.reference_solution)
        ids = tuple(str(step.get("id")) for step in derived.reference_solution)
    except Exception as exc:  # noqa: BLE001 — eval must compare every fixture
        active_gates = frozenset()
        lint_label = "ERROR"
        error = repr(exc)[:300]
        nodes = None
        ids = ()
    finally:
        if previous is None:
            os.environ.pop(KC_FLAG, None)
        else:
            os.environ[KC_FLAG] = previous

    defect_classes: set[str] = set()
    restored = os.environ.get(KC_FLAG)
    if enabled:
        os.environ[KC_FLAG] = "1"
    else:
        os.environ.pop(KC_FLAG, None)
    try:
        for raw in raw_responses:
            defect_classes |= _attempt_defects(
                fixture,
                raw,
                canonical_symbols=canonical_symbols,
                normalization_map=normalization_map,
            )
    finally:
        if restored is None:
            os.environ.pop(KC_FLAG, None)
        else:
            os.environ[KC_FLAG] = restored
    return EvalResult(
        nodes=nodes,
        defects=tuple(sorted(defect_classes)),
        retries=max(0, len(raw_responses) - 1),
        lint=lint_label,
        active_gates=tuple(sorted(active_gates)),
        ids=ids,
        error=error,
    )


def _result_cell(result: EvalResult) -> str:
    classes = ",".join(result.defects) if result.defects else "NONE"
    return f"{classes} (r{result.retries})"


def _report_fixture(fixture: Fixture, off: EvalResult, on: EvalResult) -> None:
    print(f"\n{'=' * 78}\n{fixture.name} ({fixture.corpus})\n{'=' * 78}")
    for label, result in (("OFF", off), ("ON ", on)):
        print(
            f"  {label}: nodes={result.nodes if result.nodes is not None else '-'} "
            f"defects={_result_cell(result)} lint={result.lint} "
            f"active_gates={list(result.active_gates)}"
        )
        if result.ids:
            print(f"       ids: {', '.join(result.ids)}")
        if result.error:
            print(f"       error: {result.error}")


def _print_summary(rows: list[tuple[Fixture, EvalResult, EvalResult]]) -> None:
    headers = (
        "fixture",
        "off_nodes",
        "on_nodes",
        "off_defects",
        "on_defects",
        "off_lint",
        "on_lint",
    )
    data = [
        (
            fixture.name,
            str(off.nodes if off.nodes is not None else "-"),
            str(on.nodes if on.nodes is not None else "-"),
            _result_cell(off),
            _result_cell(on),
            off.lint,
            on.lint,
        )
        for fixture, off, on in rows
    ]
    widths = [max(len(row[i]) for row in [headers, *data]) for i in range(len(headers))]

    print(f"\n{'=' * 78}\nFINAL SUMMARY\n{'=' * 78}")
    print(" | ".join(value.ljust(widths[i]) for i, value in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in data:
        print(" | ".join(value.ljust(widths[i]) for i, value in enumerate(row)))
    print("\nRaw derived graphs are expected to fail gate 2 before promote.py stamps entity_key.")


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not found (checked env + main checkout .env)")
    rows: list[tuple[Fixture, EvalResult, EvalResult]] = []
    for fixture in FIXTURES:
        off = await _run_mode(fixture, enabled=False)
        on = await _run_mode(fixture, enabled=True)
        rows.append((fixture, off, on))
        _report_fixture(fixture, off, on)
    _print_summary(rows)


if __name__ == "__main__":
    asyncio.run(main())
