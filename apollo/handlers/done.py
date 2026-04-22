"""POST /apollo/sessions/{id}/done — freeze, solve, grade, narrate, award XP."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.store import KGStore
from apollo.overseer.coverage import compute_coverage
from apollo.overseer.diagnostic import generate_diagnostic
from apollo.overseer.problem_selector import list_problems_for_cluster
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.xp import compute_xp_earned
from apollo.persistence.attempt_history import has_prior_graded_attempt
from apollo.persistence.models import ApolloSession, ProblemAttempt, SessionPhase
from apollo.persistence.progress_repo import apply_xp
from apollo.schemas.problem import Problem
from apollo.solver.forward_chain import solve_kg_against_problem
from apollo.solver.sympy_exec import _format_value_text


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


def _display_value(val) -> str | None:
    if val is None:
        return None
    return _format_value_text(val)


async def handle_done(*, db: AsyncSession, session_id: int) -> Dict[str, Any]:
    store = KGStore(db)

    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    problem = _find_problem(sess.concept_cluster_id, sess.current_problem_id)

    attempt = (
        await db.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.session_id == session_id)
            .where(ProblemAttempt.problem_id == problem.id)
            .order_by(ProblemAttempt.id.desc())
        )
    ).scalars().first()
    if attempt is None:
        raise RuntimeError(
            f"no ProblemAttempt for session {session_id} / problem {problem.id}"
        )

    await store.freeze(session_id)

    kg = await store.read_kg(attempt_id=attempt.id)
    sess.phase = SessionPhase.SOLVING.value
    await db.commit()

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

    reference_steps = [s.model_dump() for s in problem.reference_solution]
    coverage = compute_coverage(kg, reference_steps)
    rubric = compute_rubric(coverage, reference_steps)

    diagnostic_narrative = generate_diagnostic(
        coverage=coverage,
        solver_result=solver_result,
        reference_steps=reference_steps,
        problem_text=problem.problem_text,
        rubric=rubric,
    )

    solver_indicator: Dict[str, Any] = {
        "reached": solver_result["status"] == "solved",
    }
    value_str = _display_value(solver_result.get("value"))
    if value_str is not None:
        solver_indicator["value"] = value_str
    if solver_result.get("missing_variables"):
        solver_indicator["missing"] = solver_result["missing_variables"]

    # ── Phase 2: award XP based on the rubric score + difficulty.
    #
    # Re-attempt detection covers two cases:
    #   (a) Within-session retry: the /retry endpoint keeps the same
    #       ProblemAttempt row, so its `result` is already set from a
    #       previous Done call.
    #   (b) Cross-session repeat: another graded attempt exists for the
    #       same (student_id, problem_id).
    is_reattempt_in_session = attempt.result is not None
    is_reattempt_cross_session = await has_prior_graded_attempt(
        db=db,
        student_id=sess.student_id,
        problem_id=problem.id,
        exclude_attempt_id=attempt.id,
    )
    is_reattempt = is_reattempt_in_session or is_reattempt_cross_session

    xp_earned = compute_xp_earned(
        overall_score=rubric["overall"]["score"],
        difficulty=attempt.difficulty,
        is_reattempt=is_reattempt,
    )

    attempt.result = solver_result["status"]
    attempt.solver_trace = {
        "trace": _serializable_trace(solver_result["trace"]),
        "value": value_str,
        "missing_variables": solver_result.get("missing_variables", []),
    }
    attempt.diagnostic_report = {
        "narrative": diagnostic_narrative,
        "rubric": rubric,
        "coverage": coverage,
    }
    sess.phase = SessionPhase.REPORT.value
    await db.commit()

    # Apply XP after the attempt + session row updates have committed so
    # that a progress-repo failure never leaves the attempt ungraded.
    progress = await apply_xp(
        db=db,
        student_id=sess.student_id,
        xp_delta=xp_earned,
    )

    return {
        "rubric": rubric,
        "solver_indicator": solver_indicator,
        "diagnostic_narrative": diagnostic_narrative,
        "coverage": coverage,
        "xp_earned": xp_earned,
        "xp_before": progress["xp_before"],
        "xp_after": progress["xp_after"],
        "level_before": progress["level_before"],
        "level_after": progress["level_after"],
        "level_up": progress["level_up"],
    }
