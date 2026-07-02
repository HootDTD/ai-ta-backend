"""Shared scaffolding for the S1-S5 stage-audit judges (Plan Task E1).

A judge is: `build_items(raw) -> list[item]` (pure, unit-tested WITHOUT an LLM
— this is "the input-assembly logic [that] is what's under test" per the
plan), one schema-constrained LLM call per item via the injected
:class:`JudgeLLM`, and a deterministic aggregation (`pass_rate = ok / total`).

The `json_schema` shape mirrors the existing provisioning precedent
(``apollo/provisioning/provisioning_schema.py``): a strict, closed object with
every field in ``required``. All five judges share the same two-field verdict
shape (`ok: bool`, `reason: str`) so :func:`verdict_schema` is the single
schema builder — only the ``name`` and the item-specific prompt differ per
stage.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "Verdict",
    "JudgeResult",
    "JudgeLLM",
    "StageJudge",
    "verdict_schema",
    "aggregate",
    "load_jsonl",
    "OpenAIJudgeClient",
]


@dataclass(frozen=True)
class Verdict:
    """One item-level judgment. ``item_id`` must be stable/unique within a
    stage run so E3's report can cite the exact failing item."""

    item_id: str
    ok: bool
    reason: str


@dataclass(frozen=True)
class JudgeResult:
    """A stage's aggregated verdicts. Carries the raw numbers only — gate-bar
    comparison (95%, 90%, precision-only, etc.) is E3's job, not this one's,
    so the same result shape works for every stage regardless of its bar."""

    stage: str
    verdicts: tuple[Verdict, ...]
    passed: int
    total: int
    pass_rate: float
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if not v.ok)


def aggregate(verdicts: Sequence[Verdict]) -> tuple[int, int, float]:
    """Deterministic ``passed, total, pass_rate`` — the campaign gate math.
    An empty item set aggregates to ``(0, 0, 0.0)``: a stage audited on zero
    items never silently reads as "100% passing"."""
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v.ok)
    pass_rate = (passed / total) if total else 0.0
    return passed, total, pass_rate


def verdict_schema(name: str) -> dict[str, Any]:
    """Strict ``json_schema`` payload shared by every S1-S5 judge call (a
    FRESH dict per call, mirroring ``provisioning_schema.py``'s builders).
    Every judge asks the same question shape: is this item correct, and why."""
    return {
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok", "reason"],
            "properties": {
                "ok": {"type": "boolean"},
                "reason": {"type": "string"},
            },
        },
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load a `.jsonl` run-dir artifact. Missing file -> empty list (a stage
    with no recorded inputs yet, e.g. before D1-D3 land data, audits as zero
    items rather than raising)."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


class JudgeLLM(Protocol):
    """The seam every judge calls through. ``schema`` is a `verdict_schema(...)`
    payload; implementations MUST return a dict with `ok`/`reason` keys (the
    real client validates this via the API's strict json_schema; fakes in
    tests return canned dicts directly)."""

    async def judge_item(
        self, *, system_prompt: str, user_prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...


class StageJudge:
    """Base class for S1-S5. Subclasses set ``stage`` + ``system_prompt`` and
    implement ``build_items``/``user_prompt``/``item_id``. ``judge`` is the
    only method that touches the LLM — everything else here is pure and unit
    tested directly against fixtures without a network call."""

    stage: str = ""
    system_prompt: str = ""

    def __init__(self, llm: JudgeLLM):
        self._llm = llm

    def build_items(self, raw: Any) -> list[dict[str, Any]]:
        """Assemble ONLY this stage's input/output into judge-ready items.
        Pure; must not read files itself (callers load the run-dir data and
        hand it in) so it stays trivially unit testable."""
        raise NotImplementedError

    def item_id(self, item: Mapping[str, Any]) -> str:
        return str(item.get("item_id", ""))

    def user_prompt(self, item: Mapping[str, Any]) -> str:
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        return verdict_schema(f"{self.stage}_verdict")

    async def judge(self, raw: Any) -> JudgeResult:
        items = self.build_items(raw)
        verdicts: list[Verdict] = []
        schema = self.schema()
        for item in items:
            response = await self._llm.judge_item(
                system_prompt=self.system_prompt,
                user_prompt=self.user_prompt(item),
                schema=schema,
            )
            verdicts.append(
                Verdict(
                    item_id=self.item_id(item),
                    ok=bool(response.get("ok", False)),
                    reason=str(response.get("reason", "")),
                )
            )
        passed, total, pass_rate = aggregate(verdicts)
        return JudgeResult(
            stage=self.stage,
            verdicts=tuple(verdicts),
            passed=passed,
            total=total,
            pass_rate=pass_rate,
        )


class OpenAIJudgeClient:
    """Live :class:`JudgeLLM` — one schema-constrained ``gpt-4o`` chat call per
    item, mirroring the ``response_format={"type": "json_schema", ...}``
    precedent (``apollo/provisioning/orchestrator.py::_tag_mint_chat_fn``).
    The synchronous ``OpenAI`` client is offloaded to a thread so judges (an
    async pipeline) never block the event loop. Never exercised by unit tests
    (no network in CI) — the network call is pragma-excluded; the class body
    and constructor ARE covered."""

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model or os.getenv("APOLLO_JUDGE_MODEL", "gpt-4o")

    async def judge_item(
        self, *, system_prompt: str, user_prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._call, system_prompt=system_prompt, user_prompt=user_prompt, schema=schema
        )

    def _call(
        self, *, system_prompt: str, user_prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - real network call, never hit in CI
        from openai import OpenAI

        client = OpenAI()
        resp = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_schema", "json_schema": schema},
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)
