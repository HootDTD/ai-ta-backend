"""Pure conversion core for seeding the ``apollo_misconceptions`` TABLE bank
(migration 019) from the on-disk ``misconceptions.json`` authoring source.

**This is a DIFFERENT store from the ``kind='misconception'`` rows**
``apollo.persistence.learner_model_seed.misconceptions_to_entities`` mints
into ``apollo_kg_entities`` (the Layer-1 KG opposes-link graph consumed by
grading). ``apollo_misconceptions`` backs the RUNTIME misconception-inference
channel (``apollo/overseer/misconception.py`` -> ``misconception_bank.py`` ->
embedding match) and the graph-grader's soundness axis
(``grade.soundness_applicable`` is ``False`` — coverage-only fallback — when a
concept's bank is empty, per D5/D6). Both stores are seeded from the SAME
``misconceptions.json`` file; neither implies the other exists.

``misconceptions.json`` (the ``EntitySpec`` shape consumed by
``misconceptions_to_entities``) carries no ``code`` / ``probe_question`` /
``rt_steps`` / ``confusion_pair`` fields — those are ``apollo_misconceptions``-
specific authoring fields. This module derives them generically so the SAME
source file backs both stores with no additional authoring burden:

  * ``code``        <- ``key`` with the ``misc.`` prefix stripped (matches the
                       table's author-facing stable-id convention, e.g.
                       ``no_density``).
  * ``probe_question`` <- an optional authored ``probe_question`` field, else
                       a generic Socratic-voiced fallback built from
                       ``description`` (the NOT NULL column always gets a
                       usable value; never a blank string).
  * ``rt_steps``    <- an optional authored ``rt_steps`` list, else ``[]``
                       (JSONB NOT NULL DEFAULT already covers this at the
                       column level; explicit here for the pure spec).
  * ``confusion_pair_a`` / ``confusion_pair_b`` <- an optional authored
                       ``confusion_pair`` 2-tuple/list, else both ``None``
                       (the column pair is nullable — analytics-only).
  * ``trigger_phrases`` <- copied verbatim (already the right shape).

DB-free, LLM-free, embedding-free: this module returns a list of
``MisconceptionBankSpec`` (frozen) for the caller (``scripts/
seed_apollo_misconceptions.py``) to embed + upsert. Never mutates its input.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MisconceptionBankSpec:
    """A single migration-019 ``apollo_misconceptions`` row, pre-DB,
    pre-embedding. ``description_embedding`` is populated by the caller
    (this module does no network I/O)."""

    code: str
    description: str
    confusion_pair_a: str | None
    confusion_pair_b: str | None
    trigger_phrases: tuple[str, ...] = ()
    probe_question: str = ""
    rt_steps: tuple[str, ...] = field(default_factory=tuple)


def _default_probe_question(description: str) -> str:
    """A generic Socratic-voiced fallback probe when the source file has no
    authored ``probe_question``. Never empty (the column is NOT NULL)."""
    return f"Wait, I'm confused — {description} Is that really right?"


def _confusion_pair(entry: dict) -> tuple[str | None, str | None]:
    pair = entry.get("confusion_pair")
    if not pair or len(pair) != 2:
        return None, None
    return str(pair[0]), str(pair[1])


def misconception_entry_to_bank_spec(entry: dict) -> MisconceptionBankSpec:
    """Convert one ``misconceptions.json`` entry into a ``MisconceptionBankSpec``.

    Requires ``key`` and ``description`` (the entries this repo authors always
    carry both — ``misconceptions_to_entities`` already requires ``key``).
    """
    code = entry["key"].removeprefix("misc.")
    description = entry.get("description", "")
    pair_a, pair_b = _confusion_pair(entry)
    return MisconceptionBankSpec(
        code=code,
        description=description,
        confusion_pair_a=pair_a,
        confusion_pair_b=pair_b,
        trigger_phrases=tuple(entry.get("trigger_phrases", [])),
        probe_question=entry.get("probe_question") or _default_probe_question(description),
        rt_steps=tuple(entry.get("rt_steps", [])),
    )


def misconceptions_json_to_bank_specs(misc: dict) -> list[MisconceptionBankSpec]:
    """Convert a parsed ``misconceptions.json`` dict into the full list of
    ``MisconceptionBankSpec`` for one concept (order preserved)."""
    return [
        misconception_entry_to_bank_spec(entry) for entry in misc.get("misconceptions", [])
    ]


__all__ = [
    "MisconceptionBankSpec",
    "misconception_entry_to_bank_spec",
    "misconceptions_json_to_bank_specs",
]
