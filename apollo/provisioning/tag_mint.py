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
grading for every student, so minting refuses rather than guesses. The ONE
exception is a prereq edge naming an unminted entity key: prereqs are optional
KG-enrichment edges the LLM routinely draws to a problem given (not a minted
reference step), so such an edge is DROPPED (logged), not fatal — see step 5b.
NO network: ``chat_fn``/``embed_fn`` are injected (mocked in Tier-1).

Misconception-storage DEVIATION (orchestrator-signed, ADJ #2): auto-minted
misconceptions are stored as ``apollo_kg_entities kind='misconception'`` (a valid
ENTITY_KIND) via the frozen ``misconceptions_to_entities`` converter — NOT the
literal §8B.2 ``apollo_misconceptions`` table (whose NOT-NULL Socratic
``probe_question``/``rt_steps`` v1 auto-provisioning cannot responsibly author).
"""

from __future__ import annotations

import json
import logging
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
    load_concept_prereq_adjacency,
    partition_prereqs_by_concept_scope,
    resolve_or_create_concept,
    upsert_entity,
)

_LOG = logging.getLogger(__name__)

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
    # Prereq edges the LLM drafted but tag/mint refused to persist, surfaced for
    # observability (previously only INFO/WARNING-logged). Three drop reasons land
    # here in one place: an endpoint key no entity carries (unresolvable), an edge
    # that would introduce a self-loop/cycle (acyclicity guard, #73), and an edge
    # whose resolved endpoint belongs to a FOREIGN concept (endpoint guard, #74).
    dropped_prereq_pairs: list[tuple[str, str]] = []


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


def _acyclic_prereq_pairs(
    pairs: list[tuple[str, str]],
    key_to_id: dict[str, int],
    *,
    persisted_adj: dict[int, set[int]] | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Greedily keep a maximal acyclic subset of resolvable prereq key-pairs.

    Each endpoint is resolved to its entity id via ``key_to_id`` BEFORE the
    acyclicity check, so two keys that dedup MERGED onto one id (or a bare-id
    alias) — which collapse a ``from != to`` key-pair into a self-loop, or close a
    cycle, in the ID graph — are caught even though the keys differ. An edge is
    dropped when it is a self-loop (``from_id == to_id``) or when adding it would
    close a directed cycle (its target already reaches its source). Deterministic:
    pairs are processed in input order, so the earliest edge of a conflicting pair
    is kept and the later (cycle-closing) one dropped. The composite PK
    ``(from_entity_id, to_entity_id)`` permits both ``(A,B)`` and ``(B,A)`` (and a
    self-loop) with no DB-level acyclicity, so without this guard a single mint's
    drafted edges can persist a cycle.

    CROSS-MINT SEEDING (M2): ``adj`` is seeded from ``persisted_adj`` — the
    caller's ``load_concept_prereq_adjacency`` read of the SAME concept's already
    -committed ``apollo_entity_prereqs`` rows — before this mint's own edges are
    folded in. Without this seed, a SECOND mint into a shared concept could pass
    this in-mint-only check while drafting the REVERSE of an EARLIER mint's
    persisted edge (mint 1: A->B; mint 2: B->A), producing a persisted 2-cycle
    across mints that neither mint's own draft alone contains. Returns ``(kept,
    dropped)`` as key-pairs."""
    kept: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    # from_entity_id -> {to_entity_id}; seeded with persisted edges (a COPY — the
    # caller's dict/sets are never mutated) so this mint's DFS also sees what an
    # earlier mint into the same concept already committed.
    adj: dict[int, set[int]] = {
        node: set(targets) for node, targets in (persisted_adj or {}).items()
    }

    def _reaches(src: int, dst: int) -> bool:
        seen: set[int] = set()
        stack = [src]
        while stack:
            node = stack.pop()
            if node == dst:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adj.get(node, ()))
        return False

    for from_key, to_key in pairs:
        from_id = key_to_id[from_key]
        to_id = key_to_id[to_key]
        # Self-loop, or the target already reaches the source -> adding the edge
        # would introduce a cycle. Drop it.
        if from_id == to_id or _reaches(to_id, from_id):
            dropped.append((from_key, to_key))
            continue
        adj.setdefault(from_id, set()).add(to_id)
        kept.append((from_key, to_key))

    return kept, dropped


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
    # Entities resolved EARLIER in THIS mint (minted or merged). They are excluded
    # from each subsequent candidate's dedup pool so two distinct nodes of one
    # problem (the m≡M fusion) cannot merge against each other — only PRE-EXISTING
    # entities from prior mints are legitimate dedup targets (PR2 Part B).
    resolved_ids: set[int] = set()

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
            exclude_entity_ids=resolved_ids,
        )
        if verdict.verdict == "merged" and verdict.matched_entity_id is not None:
            key_to_id[spec.canonical_key] = verdict.matched_entity_id
            merged_entity_keys.append(spec.canonical_key)
            resolved_ids.add(verdict.matched_entity_id)
            continue

        entity_id, _inserted = await upsert_entity(
            db,
            concept_id=concept_id,
            spec=spec,
            scope_summary=scope_summary,
        )
        key_to_id[spec.canonical_key] = entity_id
        minted_entity_ids[spec.canonical_key] = entity_id
        resolved_ids.add(entity_id)

    # Register BARE-id aliases so an LLM prereq/opposes draft that names a
    # reference node by its bare id (bernoulli) resolves to the SAME entity as its
    # prefixed canonical_key (eq.bernoulli). The LLM never sees the prefix scheme,
    # so it authors bare ids; this alias lets a bare-id prereq/opposes edge
    # resolve to the same entity as its prefixed canonical_key. setdefault never
    # shadows a real canonical key. A genuinely-unknown key still fails for
    # ``opposes`` (5a, fail-closed) but is now DROPPED for prereqs (5b).
    for bare_id, canonical_key in _bare_id_aliases(problem).items():
        if canonical_key in key_to_id:
            key_to_id.setdefault(bare_id, key_to_id[canonical_key])

    # --- 5a. Link misconception opposes (fail-closed on an unmappable key) -- #
    try:
        await link_opposes(db, concept_id=concept_id, key_to_id=key_to_id)
    except KeyError as exc:
        raise TagMintError(f"misconception opposes an unknown entity key {exc}") from exc

    # --- 5b. Insert LLM-drafted prereq edges ------------------------------- #
    # A malformed prereq ENTRY (not a 2-tuple / {from,to}) is still fail-closed —
    # the draft's structure is corrupt. But an edge naming an UNMINTED entity key
    # is DROPPED, not fatal (intent decision, 2026-06-30): prereqs are optional
    # KG-enrichment edges, and the LLM routinely names a problem given (e.g.
    # ``pressure_box_3``) — a real quantity that is not a reference-solution step,
    # so it is never minted. An edge to a non-existent node cannot be inserted
    # anyway; dropping it keeps the (otherwise valid) problem promotable instead
    # of failing it. Scoped to prereqs ONLY — entity minting and misconception
    # ``opposes`` (5a) stay fail-closed (a mislinked entity corrupts grading).
    raw_pairs = tag.get("prereqs", []) or []
    parsed_pairs: list[tuple[str, str]] = []
    for entry in raw_pairs:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            parsed_pairs.append((str(entry[0]), str(entry[1])))
        elif isinstance(entry, dict) and "from" in entry and "to" in entry:
            parsed_pairs.append((str(entry["from"]), str(entry["to"])))
        else:
            raise TagMintError(f"prereq draft entry is malformed: {entry!r}")

    prereq_pairs: list[tuple[str, str]] = []
    dropped_pairs: list[tuple[str, str]] = []
    for from_key, to_key in parsed_pairs:
        if from_key in key_to_id and to_key in key_to_id:
            prereq_pairs.append((from_key, to_key))
        else:
            dropped_pairs.append((from_key, to_key))
    if dropped_pairs:
        _LOG.info(
            "tag_mint_dropped_unresolvable_prereqs",
            extra={
                "event": "tag_mint_dropped_unresolvable_prereqs",
                "concept_id": concept_id,
                "dropped": dropped_pairs,
            },
        )
    # Endpoint concept-scope guard (audit bug #4) — run BEFORE the acyclicity guard.
    # A dedup MERGE onto a foreign-concept entity leaves a 'resolvable' key the
    # unresolvable-drop above cannot catch. Dropping cross-concept edges FIRST keeps
    # a foreign endpoint out of the acyclicity reachability graph, where it could
    # otherwise act as a phantom BRIDGE across two cross-concept edges and fake a
    # cycle that discards a legitimate within-concept edge. Surfaced, never silent.
    prereq_pairs, cross_concept_pairs = await partition_prereqs_by_concept_scope(
        db, concept_id=concept_id, key_to_id=key_to_id, pairs=prereq_pairs
    )
    if cross_concept_pairs:
        _LOG.warning(
            "tag_mint_dropped_cross_concept_prereqs",
            extra={
                "event": "tag_mint_dropped_cross_concept_prereqs",
                "concept_id": concept_id,
                "dropped": cross_concept_pairs,
            },
        )
    # Acyclicity guard (audit bug #3): drop any resolvable prereq edge that would
    # introduce a self-loop or a directed cycle in apollo_entity_prereqs. Runs over
    # the resolved entity-id graph (key_to_id), so a dedup MERGE that collapses two
    # keys onto one id (the m≡M fusion) can't sneak a self-loop/cycle through.
    # M2: seed the acyclicity DFS from this concept's ALREADY-PERSISTED prereq
    # edges (scoped to the entity ids this mint resolved) so a CROSS-MINT cycle
    # (an earlier mint's committed A->B vs. this mint's drafted B->A into the SAME
    # shared concept) is caught too, not just a cycle contained wholly within this
    # mint's own draft (``insert_prereqs`` re-derives the full persisted graph as a
    # second, writer-boundary backstop).
    persisted_adj = await load_concept_prereq_adjacency(
        db, concept_id=concept_id, entity_ids=set(key_to_id.values())
    )
    prereq_pairs, cyclic_pairs = _acyclic_prereq_pairs(
        prereq_pairs, key_to_id, persisted_adj=persisted_adj
    )
    if cyclic_pairs:
        _LOG.info(
            "tag_mint_dropped_cyclic_prereqs",
            extra={
                "event": "tag_mint_dropped_cyclic_prereqs",
                "concept_id": concept_id,
                "dropped": cyclic_pairs,
            },
        )
    # Persist the surviving edges. ``insert_prereqs`` re-applies the concept-scope
    # guard at the writer boundary (defense-in-depth); after the pre-filter above it
    # has nothing left to drop, so its dropped-return is empty here.
    await insert_prereqs(db, concept_id=concept_id, key_to_id=key_to_id, pairs=prereq_pairs)

    return MintPlan(
        concept_id=concept_id,
        concept_slug=concept_slug,
        authored_symbols=authored_symbols,
        minted_entity_ids=minted_entity_ids,
        merged_entity_keys=merged_entity_keys,
        prereq_pairs=prereq_pairs,
        misconception_keys=misconception_keys,
        # Drop pipeline order: unresolvable-key -> cross-concept -> cyclic.
        dropped_prereq_pairs=[*dropped_pairs, *cross_concept_pairs, *cyclic_pairs],
    )
