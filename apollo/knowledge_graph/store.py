"""Knowledge Graph store — CRUD, summarization, freeze enforcement.

One store instance per AsyncSession. All writes validate schema via the
KGEntry ORM model plus the per-type content shapes defined in
apollo/schemas/problem.py.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sympy import latex

from apollo.errors import SessionFrozenError
from apollo.persistence.models import ApolloSession, KGEntry
from apollo.solver.sympy_exec import _tidy_floats, parse_zero_form

_KG_TYPES = ("equation", "definition", "condition", "simplification", "variable_mapping", "procedure_step")
_EMPTY_SUMMARY = "(the student hasn't taught me anything yet)"


def _equation_latex(symbolic: str) -> str | None:
    """Best-effort LaTeX render for display. Returns None on parse failure so the
    frontend can fall back to the raw symbolic string."""
    try:
        expr = parse_zero_form(symbolic, entry_id="_display_only")
        return latex(_tidy_floats(expr))
    except Exception:  # noqa: BLE001
        return None


class KGStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def write_entries(
        self, *, attempt_id: int, entries: List[Dict[str, Any]], source: str
    ) -> int:
        """Write KG entries under a ProblemAttempt.

        Raises SessionFrozenError if the owning session is frozen.
        Returns the number of entries written."""
        session_id = await self._session_id_for_attempt(attempt_id)
        await self._ensure_unfrozen(session_id)
        added = 0
        for e in entries:
            t = e.get("type")
            if t not in _KG_TYPES:
                continue
            self.db.add(KGEntry(
                session_id=session_id,
                attempt_id=attempt_id,
                type=t,
                content=e.get("content", {}),
                source=source,
            ))
            added += 1
        await self.db.commit()
        return added

    async def read_kg(self, *, attempt_id: int) -> Dict[str, List[Dict[str, Any]]]:
        """Return the KG for a ProblemAttempt, grouped by entry type."""
        result = await self.db.execute(
            select(KGEntry).where(KGEntry.attempt_id == attempt_id).order_by(KGEntry.id)
        )
        rows = result.scalars().all()
        kg: Dict[str, List[Dict[str, Any]]] = {t: [] for t in _KG_TYPES}
        for row in rows:
            content = dict(row.content or {})
            if row.type == "equation" and "symbolic" in content and "latex" not in content:
                tex = _equation_latex(content["symbolic"])
                if tex is not None:
                    content["latex"] = tex
            kg[row.type].append(content)
        return kg

    async def summarize_for_apollo(self, *, attempt_id: int) -> str:
        """Bullet summary for Apollo's context — student-sourced labels only."""
        kg = await self.read_kg(attempt_id=attempt_id)
        lines: List[str] = []
        for eq in kg["equation"]:
            lines.append(f"- equation ({eq.get('label', '(no label)')}): {eq.get('symbolic', '')}")
        for d in kg["definition"]:
            lines.append(f"- definition: {d.get('concept', '?')} = {d.get('meaning', '?')}")
        for c in kg["condition"]:
            lines.append(f"- condition: {c.get('applies_when', '?')}")
        for s in kg["simplification"]:
            lines.append(f"- simplification: when {s.get('applies_when', '?')}, {s.get('transformation', '?')}")
        for vm in kg["variable_mapping"]:
            lines.append(f"- variable: {vm.get('term', '?')} → {vm.get('symbol', '?')}")
        for ps in sorted(kg["procedure_step"], key=lambda p: int(p.get("order") or 0)):
            lines.append(
                f"- procedure step {ps.get('order', '?')}: {ps.get('action', '?')}"
            )
        return "\n".join(lines) if lines else _EMPTY_SUMMARY

    async def _session_id_for_attempt(self, attempt_id: int) -> int:
        from apollo.persistence.models import ProblemAttempt
        row = await self.db.execute(
            select(ProblemAttempt.session_id).where(ProblemAttempt.id == attempt_id)
        )
        sid = row.scalar_one_or_none()
        if sid is None:
            raise ValueError(f"attempt {attempt_id} not found")
        return sid

    async def _ensure_unfrozen(self, session_id: int) -> None:
        result = await self.db.execute(
            select(ApolloSession.phase).where(ApolloSession.id == session_id)
        )
        phase = result.scalar_one_or_none()
        if phase in ("PROBLEM_REVEAL", "SOLVING", "REPORT"):
            raise SessionFrozenError(session_id=str(session_id))

    async def freeze(self, session_id: int) -> None:
        result = await self.db.execute(
            select(ApolloSession).where(ApolloSession.id == session_id)
        )
        session = result.scalar_one()
        session.phase = "PROBLEM_REVEAL"
        await self.db.commit()

    async def unfreeze(self, session_id: int) -> None:
        result = await self.db.execute(
            select(ApolloSession).where(ApolloSession.id == session_id)
        )
        session = result.scalar_one()
        session.phase = "TEACHING"
        await self.db.commit()
