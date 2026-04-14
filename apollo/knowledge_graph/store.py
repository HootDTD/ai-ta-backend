"""Knowledge Graph store — CRUD, summarization, freeze enforcement.

One store instance per AsyncSession. All writes validate schema via the
KGEntry ORM model plus the per-type content shapes defined in
apollo/schemas/problem.py.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import SessionFrozenError
from apollo.persistence.models import ApolloSession, KGEntry

_KG_TYPES = ("equation", "definition", "condition", "simplification", "variable_mapping")
_EMPTY_SUMMARY = "(the student hasn't taught me anything yet)"


class KGStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def write_entries(
        self, session_id: int, entries: List[Dict[str, Any]], *, source: str
    ) -> int:
        """Write KG entries. Raises SessionFrozenError if the session is frozen.
        Returns the number of entries written."""
        await self._ensure_unfrozen(session_id)
        added = 0
        for e in entries:
            t = e.get("type")
            if t not in _KG_TYPES:
                continue
            self.db.add(KGEntry(
                session_id=session_id,
                type=t,
                content=e.get("content", {}),
                source=source,
            ))
            added += 1
        await self.db.commit()
        return added

    async def read_kg(self, session_id: int) -> Dict[str, List[Dict[str, Any]]]:
        """Return the KG grouped by entry type."""
        result = await self.db.execute(
            select(KGEntry).where(KGEntry.session_id == session_id).order_by(KGEntry.id)
        )
        rows = result.scalars().all()
        kg: Dict[str, List[Dict[str, Any]]] = {t: [] for t in _KG_TYPES}
        for row in rows:
            kg[row.type].append(row.content)
        return kg

    async def summarize_for_apollo(self, session_id: int) -> str:
        """Bullet summary for Apollo's context — student-sourced labels only."""
        kg = await self.read_kg(session_id)
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
        return "\n".join(lines) if lines else _EMPTY_SUMMARY

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
