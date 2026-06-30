"""WU-3B2d stage 4 — tag/mint: author canonical symbols + mint reference graph.

Given an ALREADY-approved ``(question, reference_solution)`` pair (an
``ApprovedPair``; 3B2e produces a compatible value later, 3B2g wires the runtime
order), ``tag_and_mint``:

  1. LLM-drafts the concept tag (slug + display name) + prereq edges via the
     injected ``chat_fn`` (cheap_chat-shaped, MOCKED in Tier-1);
  2. resolves the slug to a BIGINT ``apollo_concepts.id`` (creating it if absent);
  3. AUTHORS the concept's ``canonical_symbols``/``normalization_map`` from the
     approved problem's symbol set (first-writer-wins UNION — NOT derived from a
     promoted problem, which is circular because gate 4 runs BEFORE promotion);
  4. mints reference + misconception ``EntitySpec``s by REUSING two frozen §8 seed
     converters (``reference_solution_to_entities`` /
     ``misconceptions_to_entities``), resolving EACH entity candidate through
     3B2c's ``resolve_candidate`` dedup ladder BEFORE upsert (a ``merged`` verdict
     reuses the matched id instead of inserting);
  5. inserts the prereq edges from the LLM tag draft (NOT from the frozen
     ``concept_dag_to_prereqs`` converter — auto-provisioning drafts prereqs at
     tag time, before any concept-DAG exists) and links each misconception's
     ``opposes_entity_key`` to its entity id.

The two §8 converters that the seed script uses but this auto-provisioning path
does NOT — ``concept_dag_to_prereqs`` (prereqs are LLM-drafted here) and
``annotate_reference_solution`` (a promotion-time annotation, applied by 3B2g,
not at mint) — are intentionally unused.

Returns a typed ``MintPlan`` (observability + the 3B2g handoff). NO promotion
here — 3B2g runs ``run_promotion_lint`` over the result, flips Tier-2, and
projects ``:Canon``. FAIL-CLOSED: a hallucinated/unmappable LLM tag or an
``opposes_entity_key`` resolving to no entity raises ``TagMintError`` (mirroring
``SeedError``'s NO-FALLBACK convention) — a mislinked entity silently corrupts
grading for every student, so minting refuses rather than guesses. NO network:
``chat_fn``/``embed_fn`` are injected (mocked in Tier-1).

Misconception-storage DEVIATION (orchestrator-signed, ADJ #2): auto-minted
misconceptions are stored as ``apollo_kg_entities kind='misconception'`` (a valid
ENTITY_KIND) via the frozen ``misconceptions_to_entities`` converter — NOT the
literal §8B.2 ``apollo_misconceptions`` table (whose NOT-NULL Socratic
``probe_question``/``rt_steps`` v1 auto-provisioning cannot responsibly author).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel
from sympy import sympify

from apollo.persistence.learner_model_seed import (
    EntitySpec,
    _entity_key_for_step,
    misconceptions_to_entities,
    reference_solution_to_entities,
)
from apollo.provisioning.dedup import resolve_candidate
from apollo.provisioning.tag_mint_persist import (
    author_concept_symbols,
    insert_prereqs,
    link_opposes,
    resolve_or_create_concept,
    upsert_entity,
)

__all__ = ["ApprovedPair", "MintPlan", "TagMintError", "tag_and_mint"]


class TagMintError(RuntimeError):
    """Raised when tag/mint cannot proceed without guessing (a hallucinated /
    unmappable LLM concept tag, or an ``opposes_entity_key`` resolving to no
    entity). FAIL-CLOSED — the caller marks the run failed; no partial mint is
    committed (the caller owns the transaction)."""


class ApprovedPair(BaseModel):
    """The 3B2e output shape ``tag_and_mint`` consumes (3B2e produces a compatible
    value LATER; tested here with a hand-built fixture). An approved
    ``(question, reference_solution)`` pair plus the resolved scope."""

    problem: dict
    search_space_id: int
    solution_source: str  # 'extracted' | 'generated'
    misconceptions: list[dict] = []


class MintPlan(BaseModel):
    """Typed result enumerating everything ``tag_and_mint`` did (observability +
    the 3B2g handoff). NO promotion here — 3B2g runs the lint over this."""

    concept_id: int
    concept_slug: str
    authored_symbols: list[str]
    minted_entity_ids: dict[str, int]
    merged_entity_keys: list[str]
    prereq_pairs: list[tuple[str, str]]
    misconception_keys: list[str]


def _judge_distinct(*_a, **_k) -> str:
    """The dedup ladder's LLM-judge tier for the in-band (0.82<=cos<0.92) case.
    tag/mint never escalates a band tiebreak to a second model in v1 — a near-but-
    not-merge candidate mints as DISTINCT (the conservative, non-mislinking
    direction). 3B2f/3B2g may inject a real judge later."""
    return "distinct"


def _equation_symbols(problem: dict) -> set[str]:
    """Free-symbol names across every equation step's ``symbolic`` expression.
    Best-effort: a malformed expression contributes no symbols (gate 6 owns that
    verdict at promotion). Used to AUTHOR the concept's canonical symbol set."""
    symbols: set[str] = set()
    for step in problem.get("reference_solution", []):
        if step.get("entry_type") != "equation":
            continue
        symbolic = (step.get("content") or {}).get("symbolic")
        if not symbolic:
            continue
        try:
            expr = sympify(symbolic)
        except Exception:  # noqa: BLE001 - any sympy parse failure → no symbols
            continue
        symbols |= {s.name for s in expr.free_symbols}
    return symbols


def _normalize_symbol_base(name: str) -> str:
    """Strip a trailing digit run so ``P1``/``P2`` author the same canonical base
    ``P`` (mirrors the gate-4 ``_normalize_symbol`` subscript rule)."""
    return re.sub(r"\d+$", "", name)


def _author_symbol_set(problem: dict) -> tuple[list[str], dict[str, str]]:
    """Derive the canonical symbol set + normalization map from the approved
    problem: ``given_values`` keys + ``target_unknown`` + equation free-symbols,
    each reduced to its subscript base. The normalization map records each raw
    (subscripted) symbol -> its base so gate-4 can normalize ``P1`` -> ``P``.
    Deterministic + path-independent (same problem -> same set)."""
    raw: set[str] = set(problem.get("given_values", {}).keys())
    target = problem.get("target_unknown")
    if target:
        raw.add(target)
    raw |= _equation_symbols(problem)

    canonical: set[str] = set()
    normalization: dict[str, str] = {}
    for name in raw:
        base = _normalize_symbol_base(name)
        canonical.add(base)
        if base != name:
            normalization[name] = base
    return sorted(canonical), normalization


def _scope_summary_for(spec: EntitySpec) -> str:
    """Compose the dedup candidate's ``scope_summary`` text (the embedding source)
    from the entity's ``display_name`` + its canonical symbols (ADJ #11). Academic
    concept text — NO student PII."""
    symbol = (spec.payload or {}).get("symbol")
    pieces = [spec.display_name]
    if symbol:
        pieces.append(f"symbol {symbol}")
    pieces.append(f"kind {spec.kind}")
    return " | ".join(str(p) for p in pieces)


class _DedupCandidate:
    """The ``{canonical_key, scope_summary}`` duck-type ``resolve_candidate``
    reads (dedup.py:165). NOT a Pydantic model — a tiny adapter."""

    __slots__ = ("canonical_key", "scope_summary")

    def __init__(self, canonical_key: str, scope_summary: str) -> None:
        self.canonical_key = canonical_key
        self.scope_summary = scope_summary


def _parse_tag(chat_fn: Callable[..., str], problem: dict) -> dict[str, Any]:
    """Call the injected ``chat_fn`` for the concept tag + prereq draft and parse
    the JSON. FAIL-CLOSED: a malformed/empty response or a missing required field
    raises ``TagMintError`` (NO silent mislink)."""
    raw = chat_fn(json.dumps(problem))
    try:
        tag = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TagMintError(f"concept-tag LLM response is not JSON: {exc}") from exc
    if not isinstance(tag, dict):
        raise TagMintError("concept-tag LLM response is not a JSON object")
    slug = tag.get("concept_slug")
    if not slug or not isinstance(slug, str):
        raise TagMintError("concept-tag LLM response is missing 'concept_slug'")
    return tag


def _bare_id_aliases(problem: dict) -> dict[str, str]:
    """Map each reference-step BARE id to its PREFIXED canonical_key.

    The LLM tag prompt (orchestrator._TAG_MINT_SYSTEM_PROMPT) never reveals the
    canonical-key prefix scheme, so the model drafts prereq/opposes edges using
    the bare reference-node id (``bernoulli``) rather than the minted canonical
    key (``eq.bernoulli``). This recovers ``bare -> {prefix}.{id}`` by REUSING
    the frozen §8 ``_entity_key_for_step``, so the alias is byte-identical to what
    ``reference_solution_to_entities`` minted. A step whose ``entry_type`` is
    outside the frozen mint map is skipped (gate 1 fails it closed at promotion)."""
    aliases: dict[str, str] = {}
    for step in problem.get("reference_solution", []):
        try:
            aliases[step["id"]] = _entity_key_for_step(step)
        except (KeyError, TypeError):
            continue
    return aliases


async def tag_and_mint(
    db,
    pair: ApprovedPair,
    *,
    chat_fn: Callable[..., str],
    embed_fn: Callable[[str], Sequence[float]],
) -> MintPlan:
    """Tag the concept, author its canonical symbols, dedup + mint the reference
    and misconception entities, insert the drafted prereq edges, and return a
    ``MintPlan``. See the module docstring for the full contract.

    All persistence keys on the resolved BIGINT concept id (never the slug). A
    ``merged`` dedup verdict reuses the matched entity id; a ``distinct`` verdict
    upserts a fresh entity. FAIL-CLOSED via ``TagMintError``."""
    problem = pair.problem
    search_space_id = pair.search_space_id

    tag = _parse_tag(chat_fn, problem)
    concept_slug = tag["concept_slug"]
    display_name = tag.get("display_name") or concept_slug

    concept_id = await resolve_or_create_concept(
        db,
        search_space_id=search_space_id,
        slug=concept_slug,
        display_name=display_name,
    )

    # --- 3. Author canonical symbols (gate-4 non-vacuity) ------------------- #
    symbols, normalization = _author_symbol_set(problem)
    authored_symbols = await author_concept_symbols(
        db,
        concept_id=concept_id,
        symbols=symbols,
        normalization=normalization,
    )

    # --- 4. Mint reference + misconception entities (frozen converters) ----- #
    ref_specs = reference_solution_to_entities(problem)
    misc_specs = misconceptions_to_entities({"misconceptions": pair.misconceptions})
    all_specs: list[EntitySpec] = [*ref_specs, *misc_specs]

    key_to_id: dict[str, int] = {}
    minted_entity_ids: dict[str, int] = {}
    merged_entity_keys: list[str] = []
    misconception_keys: list[str] = [s.canonical_key for s in misc_specs]

    for spec in all_specs:
        scope_summary = _scope_summary_for(spec)
        candidate = _DedupCandidate(spec.canonical_key, scope_summary)
        verdict = await resolve_candidate(
            db,
            search_space_id=search_space_id,
            concept_id=concept_id,
            candidate=candidate,
            embed_fn=embed_fn,
            judge_fn=_judge_distinct,
        )
        if verdict.verdict == "merged" and verdict.matched_entity_id is not None:
            key_to_id[spec.canonical_key] = verdict.matched_entity_id
            merged_entity_keys.append(spec.canonical_key)
            continue

        entity_id, _inserted = await upsert_entity(
            db,
            concept_id=concept_id,
            spec=spec,
            scope_summary=scope_summary,
        )
        key_to_id[spec.canonical_key] = entity_id
        minted_entity_ids[spec.canonical_key] = entity_id

    # Register BARE-id aliases so an LLM prereq/opposes draft that names a
    # reference node by its bare id (bernoulli) resolves to the SAME entity as its
    # prefixed canonical_key (eq.bernoulli). The LLM never sees the prefix scheme,
    # so it authors bare ids; without this the hard key_to_id[...] lookup in
    # insert_prereqs / link_opposes KeyErrors and the whole document aborts (the
    # BLOCKER). setdefault never shadows a real canonical key. A genuinely-unknown
    # key (in neither the canonical nor the bare set) still raises KeyError ->
    # TagMintError (fail-closed) downstream.
    for bare_id, canonical_key in _bare_id_aliases(problem).items():
        if canonical_key in key_to_id:
            key_to_id.setdefault(bare_id, key_to_id[canonical_key])

    # --- 5a. Link misconception opposes (fail-closed on an unmappable key) -- #
    try:
        await link_opposes(db, concept_id=concept_id, key_to_id=key_to_id)
    except KeyError as exc:
        raise TagMintError(f"misconception opposes an unknown entity key {exc}") from exc

    # --- 5b. Insert LLM-drafted prereq edges (fail-closed on a bad key) ----- #
    raw_pairs = tag.get("prereqs", []) or []
    prereq_pairs: list[tuple[str, str]] = []
    for entry in raw_pairs:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            prereq_pairs.append((str(entry[0]), str(entry[1])))
        elif isinstance(entry, dict) and "from" in entry and "to" in entry:
            prereq_pairs.append((str(entry["from"]), str(entry["to"])))
        else:
            raise TagMintError(f"prereq draft entry is malformed: {entry!r}")
    try:
        await insert_prereqs(db, key_to_id=key_to_id, pairs=prereq_pairs)
    except KeyError as exc:
        raise TagMintError(f"prereq draft references an unminted entity key {exc}") from exc

    return MintPlan(
        concept_id=concept_id,
        concept_slug=concept_slug,
        authored_symbols=authored_symbols,
        minted_entity_ids=minted_entity_ids,
        merged_entity_keys=merged_entity_keys,
        prereq_pairs=prereq_pairs,
        misconception_keys=misconception_keys,
    )
