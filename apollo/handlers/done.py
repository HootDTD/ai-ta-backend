"""POST /apollo/sessions/{id}/done — freeze, solve, narrate, diagnose."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.store import KGStore
from apollo.overseer.coverage import compute_coverage
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.problem_selector import list_problems_for_cluster
from apollo.persistence.models import ApolloSession, ProblemAttempt, SessionPhase
from apollo.schemas.problem import Problem
from apollo.solver.forward_chain import solve_kg_against_problem
from apollo.solver.narrator import narrate_trace


def _find_problem(cluster_id: str, problem_id: str) -> Problem:
    for p in list_problems_for_cluster(cluster_id):
        if p.id == problem_id:
            return p
    raise RuntimeError(f"problem {problem_id!r} not in bank for cluster {cluster_id!r}")


def _serializable_trace(trace: list) -> list:
    out = []
    for entry in trace:
        out.append({k: (str(v) if k == "value" else v) for k, v in entry.items()})
    return out


async def handle_done(*, db: AsyncSession, session_id: int) -> Dict[str, Any]:
    store = KGStore(db)

    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    problem = _find_problem(sess.concept_cluster_id, sess.current_problem_id)

    await store.freeze(session_id)

    kg = await store.read_kg(session_id)
    sess.phase = SessionPhase.SOLVING.value
    await db.commit()

    # Augment problem givens with physical constants and problem-encoded
    # simplifications (e.g., horizontal pipe → h1 = h2 = 0). These come from
    # the problem setup, not the student's teaching.
    augmented_givens = dict(problem.given_values)
    augmented_givens.setdefault("g", 9.81)
    for ref in problem.reference_solution:
        if ref.entry_type == "simplification":
            aw = (ref.content.get("applies_when") or "").lower().replace(" ", "")
            if "h1==h2" in aw:
                augmented_givens.setdefault("h1", 0.0)
                augmented_givens.setdefault("h2", 0.0)

    solver_result = solve_kg_against_problem(kg, {
        "id": problem.id,
        "given_values": augmented_givens,
        "target_unknown": problem.target_unknown,
    })

    narrated = narrate_trace(
        solver_result["trace"],
        status=solver_result["status"],
        target=problem.target_unknown,
        missing_variables=solver_result.get("missing_variables"),
    )

    reference_steps = [s.model_dump() for s in problem.reference_solution]
    coverage = compute_coverage(kg, reference_steps)

    diagnostic = generate_diagnostic(
        coverage=coverage,
        solver_result=solver_result,
        reference_steps=reference_steps,
        problem_text=problem.problem_text,
    )

    attempt = (
        await db.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.session_id == session_id)
            .where(ProblemAttempt.problem_id == problem.id)
            .order_by(ProblemAttempt.id.desc())
        )
    ).scalars().first()
    attempt.result = solver_result["status"]
    attempt.solver_trace = {
        "trace": _serializable_trace(solver_result["trace"]),
        "value": str(solver_result.get("value")) if solver_result.get("value") is not None else None,
        "missing_variables": solver_result.get("missing_variables", []),
    }
    attempt.diagnostic_report = {"text": diagnostic, "coverage": coverage}
    sess.phase = SessionPhase.REPORT.value
    await db.commit()

    return {
        "result": solver_result["status"],
        "value": str(solver_result.get("value")) if solver_result.get("value") is not None else None,
        "missing_variables": solver_result.get("missing_variables", []),
        "narrated_trace": narrated,
        "diagnostic_report": diagnostic,
        "coverage": coverage,
    }
