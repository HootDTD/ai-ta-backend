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
projects ``:Canon``. FAIL-CLOSED: a hallucinated/unmappable LLM tag or a
PRE-EXISTING misconception's ``opposes_entity_key`` resolving to no entity
raises ``TagMintError`` (mirroring ``SeedError``'s NO-FALLBACK convention) — a
mislinked entity silently corrupts grading for every student, so minting
refuses rather than guesses. THIS mint's own misconception with an unlinkable
``opposes_entity_key`` is instead DROPPED with a log (2026-07-14 — no wrong
link is created, one enrichment edge is lost, the candidate stays promotable).
The other exception is a prereq edge naming an unminted entity key: prereqs are optional
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
from dataclasses import replace
from typing import Any

from pydantic import BaseModel
from sympy import expand, sympify

from apollo.persistence.learner_model_seed import (
    EntitySpec,
    _entity_key_for_step,
    misconceptions_to_entities,
    reference_solution_to_entities,
)
from apollo.persistence.models import Concept, KGEntity
from apollo.provisioning.dedup import resolve_candidate
from apollo.provisioning.tag_mint_persist import (
    author_concept_symbols,
    drop_unlinkable_minted_misconceptions,
    insert_prereqs,
    link_opposes,
    load_concept_entities,
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


class ResolvedConcept(BaseModel):
    """A PRE-MATCHED course concept (reversed provisioning): ``tag_and_mint``
    uses it verbatim — no LLM tag draft, no concept creation, and prereq edges
    derived deterministically from the reference graph's ``depends_on``. The
    concept row must already exist (premade list); a missing id is a
    fail-closed ``TagMintError``."""

    concept_id: int
    slug: str


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
    # Reference-entity keys collapsed at mint time by the deterministic content-
    # equivalence pass (G4.3): near-identical candidates minted from ONE authored
    # set (e.g. eq.eq_motion / eq.eq_velocity_formula / eq.eq1, all == the same
    # ``v = v0 + a*t``) fold onto ONE representative before the dedup ladder runs.
    # These keys did NOT mint a row; each aliases to its representative's entity id
    # so a prereq/opposes edge naming a collapsed duplicate still resolves.
    collapsed_entity_keys: list[str] = []
    # Prereq edges the LLM drafted but tag/mint refused to persist, surfaced for
    # observability (previously only INFO/WARNING-logged). Three drop reasons land
    # here in one place: an endpoint key no entity carries (unresolvable), an edge
    # that would introduce a self-loop/cycle (acyclicity guard, #73), and an edge
    # whose resolved endpoint belongs to a FOREIGN concept (endpoint guard, #74).
    dropped_prereq_pairs: list[tuple[str, str]] = []
    concept_symbol_diagnostic: str | None = None


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


def _normalize_equation(symbolic: str) -> str:
    """Canonicalize an equation string so two extractions of the SAME physics
    collapse but distinct equations do not (G4.3).

    The equation is moved to one side (``lhs - rhs``), ``expand``-ed, and reduced
    to a sign-canonical string (``min(str(expr), str(-expr))``) so ``v = v0 + a*t``,
    ``v0 + a*t = v`` and ``v - v0 - a*t`` all map to ONE key. CASE-SENSITIVE by
    construction — sympy treats ``m`` and ``M`` as DISTINCT symbols, so this can
    never reintroduce the 2026-06-30 ``m≡M`` false-merge. A chained equality
    (``a = b = c``, the campaign's Finding-A notation) or any expression sympy
    cannot parse falls back to a whitespace-stripped raw string (deterministic,
    case-preserved) rather than guessing."""
    raw = str(symbolic).strip()
    try:
        if raw.count("=") == 1:
            lhs, rhs = raw.split("=")
            expr = sympify(lhs) - sympify(rhs)
        elif "=" not in raw:
            expr = sympify(raw)
        else:  # 2+ '=' → chained equality, not a single equation
            raise ValueError("chained equality is not a single equation")
        expr = expand(expr)
        pos, neg = str(expr), str(expand(-expr))
        return pos if pos <= neg else neg
    except Exception:  # noqa: BLE001 — any parse failure → deterministic raw fallback
        return re.sub(r"\s+", "", raw)


def _equivalence_signature(spec: EntitySpec | KGEntity) -> tuple[str, str, str]:
    """The deterministic, CASE-SENSITIVE equivalence key content-duplicate entities
    are folded on (G4.3). Computed identically for a fresh ``EntitySpec`` candidate
    AND a PERSISTED ``KGEntity`` row (both expose ``kind``/``display_name``/
    ``payload``), so it drives BOTH the within-mint collapse and the cross-mint
    content-equality pre-match against prior uploads' entities.

    ``(kind, basis, content)`` where ``basis`` names WHICH content signal decided
    (``'equation'`` | ``'symbol'`` | ``'name'`` — NOT a dedup-ladder tier) and
    ``content`` is, in priority order: the NORMALIZED equation (equation candidates
    carry ``symbolic``/``equation``), else the RAW symbol (variable candidates —
    case-sensitive, subscripts preserved so ``m≠M`` and ``p1≠p2``), else a
    whitespace-collapsed (case-preserved) display_name. ``kind`` is part of the key
    so same-text different-role candidates (an equation vs a definition) never fuse.
    Being pure content-equality (no embedding, no fuzzy) it CANNOT reintroduce the
    2026-06-30 ``m≡M`` false-merge; concept scope is enforced by the CALLER (the
    within-mint collapse compares one call's specs; the cross-mint match loads only
    THIS ``concept_id``'s persisted rows)."""
    payload = spec.payload or {}
    symbolic = payload.get("symbolic") or payload.get("equation")
    if symbolic:
        return (spec.kind, "equation", _normalize_equation(str(symbolic)))
    symbol = payload.get("symbol")
    if symbol:
        return (spec.kind, "symbol", str(symbol))  # CASE-SENSITIVE: m != M
    return (spec.kind, "name", re.sub(r"\s+", " ", str(spec.display_name)).strip())


def _collapse_equivalent_candidates(
    specs: Sequence[EntitySpec], *, page_ref: object = None
) -> tuple[list[EntitySpec], dict[str, str]]:
    """Collapse content-equivalent reference candidates minted from ONE authored
    set into a single representative each (G4.3, fix contract item 1).

    Candidates are grouped by :func:`_equivalence_signature`; each group keeps its
    FIRST (earliest, deterministic input order) member as the representative and
    folds the rest away. The representative's payload gains:

    * ``provenance`` — one ``{node_id, page_ref, scraped_label}`` triple per
      contributing candidate (the S2 shape), so a merge never loses WHICH steps /
      page contributed (fix contract item 1: preserve provenance);
    * ``equation`` — the source equation string for ``kind='equation'``
      representatives, so a downstream consumer / the S2 audit can disambiguate an
      equation role that was previously an empty-payload guess (fix contract item 2,
      Finding E).

    Returns ``(collapsed_specs, alias_map)`` where ``alias_map`` maps each DROPPED
    duplicate's ``canonical_key`` to its representative's ``canonical_key`` so the
    caller can re-point prereq/opposes edges naming a duplicate onto the survivor.
    Being purely deterministic content-equality (no embedding), this cannot
    reintroduce the ``m≡M`` embedding false-merge class."""
    groups: dict[tuple[str, str, str], list[EntitySpec]] = {}
    order: list[tuple[str, str, str]] = []
    for spec in specs:
        sig = _equivalence_signature(spec)
        if sig not in groups:
            groups[sig] = []
            order.append(sig)
        groups[sig].append(spec)

    collapsed: list[EntitySpec] = []
    alias_map: dict[str, str] = {}
    for sig in order:
        members = groups[sig]
        rep = members[0]
        provenance = [
            {
                "node_id": m.canonical_key.split(".", 1)[-1],
                "page_ref": page_ref,
                "scraped_label": m.display_name,
            }
            for m in members
        ]
        new_payload = dict(rep.payload or {})
        new_payload["provenance"] = provenance
        equation = new_payload.get("symbolic") or new_payload.get("equation")
        if rep.kind == "equation" and equation:
            new_payload["equation"] = equation
        collapsed.append(replace(rep, payload=new_payload))
        for m in members[1:]:
            alias_map[m.canonical_key] = rep.canonical_key
        if len(members) > 1:
            _LOG.info(
                "tag_mint_collapsed_equivalent_candidates",
                extra={
                    "event": "tag_mint_collapsed_equivalent_candidates",
                    "representative": rep.canonical_key,
                    "collapsed": [m.canonical_key for m in members[1:]],
                    "signature_basis": sig[1],
                },
            )
    return collapsed, alias_map


def _scope_summary_for(spec: EntitySpec) -> str:
    """Compose the dedup candidate's ``scope_summary`` text (the embedding source).

    For an EQUATION candidate the summary is the NORMALIZED equation + kind (NOT the
    display_name), so two extractions of the same equation under different labels
    produce a BYTE-IDENTICAL summary the embedding tier merges across mints, while a
    genuinely different equation gets a different summary (G4.3, Finding D). Non-
    equation candidates keep the display_name (+ symbol) summary (ADJ #11) unchanged
    — this summary path DELIBERATELY does not touch the variable path (PR#74's
    concept-scoped m/M guard owns the FUZZY tier's discrimination; stripping the
    display_name here would feed the embedder near-identical ``symbol m``/``symbol
    M`` texts and risk re-merging them). Cross-UPLOAD non-equation duplicates are
    instead caught by ``tag_and_mint``'s deterministic content-equality pre-match
    (``_equivalence_signature`` vs prior-upload rows), which never runs the embedder.
    Academic concept text — NO student PII."""
    payload = spec.payload or {}
    equation = payload.get("symbolic") or payload.get("equation")
    if spec.kind == "equation" and equation:
        return f"equation {_normalize_equation(str(equation))} | kind equation"
    symbol = payload.get("symbol")
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
    resolved_concept: ResolvedConcept | None = None,
    diagnose_existing_symbols: bool = False,
) -> MintPlan:
    """Tag the concept, author its canonical symbols, dedup + mint the reference
    and misconception entities, insert the drafted prereq edges, and return a
    ``MintPlan``. See the module docstring for the full contract.

    All persistence keys on the resolved BIGINT concept id (never the slug). A
    ``merged`` dedup verdict reuses the matched entity id; a ``distinct`` verdict
    upserts a fresh entity. FAIL-CLOSED via ``TagMintError``."""
    problem = pair.problem
    search_space_id = pair.search_space_id

    if resolved_concept is not None:
        # Reversed provisioning: the closed-list matcher already resolved the
        # concept against the course's PREMADE list — no LLM tag draft, no
        # concept creation. The registered row must exist (fail-closed).
        concept_row = await db.get(Concept, resolved_concept.concept_id)
        if concept_row is None:
            raise TagMintError(
                f"resolved concept {resolved_concept.concept_id} not found — "
                "reversed provisioning requires a registered premade concept"
            )
        concept_id = int(concept_row.id)
        concept_slug = resolved_concept.slug
        tag: dict = {}  # no draft: prereqs are graph-derived below
    else:
        tag = _parse_tag(chat_fn, problem)
        concept_slug = tag["concept_slug"]
        display_name = tag.get("display_name") or concept_slug

        concept_id = await resolve_or_create_concept(
            db,
            search_space_id=search_space_id,
            slug=concept_slug,
            display_name=display_name,
        )

    concept_row = await db.get(Concept, concept_id)
    canonical_before = (
        set(dict(concept_row.canonical_symbols or {}).get("symbols") or [])
        if diagnose_existing_symbols
        else set()
    )
    if canonical_before:
        from apollo.provisioning.promotion_lint import concept_symbol_diagnostic

        symbol_diagnostic = concept_symbol_diagnostic(
            problem,
            canonical_symbols=canonical_before,
            normalization_map=dict(concept_row.normalization_map or {}),
        )
    else:
        symbol_diagnostic = None

    # --- 3. Author canonical symbols (gate-4 non-vacuity) ------------------- #
    symbols, normalization = _author_symbol_set(problem)
    authored_symbols = await author_concept_symbols(
        db,
        concept_id=concept_id,
        symbols=symbols,
        normalization=normalization,
    )

    # --- 4. Mint reference + misconception entities (frozen converters) ----- #
    # Collapse content-equivalent reference candidates minted from THIS authored set
    # onto one representative each BEFORE the dedup ladder (G4.3): the frozen scrape
    # converter emits one spec per reference step, so two extractions of the same
    # equation/quantity arrive as separate synthetic keys the slug/embedding ladder
    # would leave as duplicates. Deterministic + case-sensitive → no ``m≡M`` fusion.
    page_ref = (problem.get("provenance") or {}).get("page")
    ref_specs = reference_solution_to_entities(problem)
    ref_specs, collapse_alias = _collapse_equivalent_candidates(ref_specs, page_ref=page_ref)
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

    # Cross-mint deterministic content-equality pre-match (G4.3, cross-UPLOAD half of
    # Finding D). A candidate whose ``_equivalence_signature`` EXACTLY matches an
    # entity a PRIOR upload minted into this concept resolves to that entity
    # deterministically — same case-sensitive, kind-in-key, concept-scoped key as the
    # within-mint collapse, so NO embedding + NO fuzzy runs and ``m≡M`` can never
    # merge (distinct signatures). This closes the cross-upload duplication the
    # display-name ``scope_summary`` leaves as embedding-tier misses for non-equation
    # kinds (def/varmap/proc/simp) when a second upload labels the same content
    # differently. The pool is a PRE-mint snapshot (rows minted in THIS call are
    # absent), so it never fuses two nodes of one problem; ``setdefault`` keeps the
    # earliest (lowest-id) entity as the survivor (first-writer-wins), matching the
    # ladder. Content-matched candidates skip ``resolve_candidate`` (hence write no
    # ``apollo_dedup_decisions`` row — like the within-mint collapse) and surface on
    # ``MintPlan.merged_entity_keys``.
    prior_by_signature: dict[tuple[str, str, str], int] = {}
    for _ent in await load_concept_entities(db, concept_id=concept_id):
        prior_by_signature.setdefault(_equivalence_signature(_ent), int(_ent.id))

    for spec in all_specs:
        prior_id = prior_by_signature.get(_equivalence_signature(spec))
        if prior_id is not None:
            key_to_id[spec.canonical_key] = prior_id
            merged_entity_keys.append(spec.canonical_key)
            resolved_ids.add(prior_id)
            continue

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

    # Register COLLAPSE aliases so a prereq/opposes edge naming a duplicate that was
    # folded away at mint time (G4.3) resolves to its surviving representative's
    # entity id (both its prefixed canonical_key AND — via the bare-id loop below —
    # its bare reference-node id). Without this, an edge to a collapsed duplicate
    # would name an unminted key and be dropped (5b).
    for dropped_key, rep_key in collapse_alias.items():
        if rep_key in key_to_id:
            key_to_id.setdefault(dropped_key, key_to_id[rep_key])

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

    # --- 5a. Link misconception opposes ------------------------------------- #
    # An opposes edge naming an unminted key DROPS the offending misconception
    # (THIS mint's rows only) instead of failing the candidate (staging set 12:
    # the LLM draft opposed a phantom procedure step 'proc.proc_explain_causality'
    # on 2/19 candidates, each rejecting an otherwise-valid problem). Dropping
    # creates no wrong link — it loses one enrichment edge — mirroring the 5b
    # prereq-drop policy. A PRE-EXISTING row with an unresolvable key keeps the
    # fail-closed contract below: it was linked by an earlier mint, and silently
    # unlinking or deleting it would corrupt grading, not enrich it.
    dropped_misconceptions = await drop_unlinkable_minted_misconceptions(
        db,
        concept_id=concept_id,
        key_to_id=key_to_id,
        minted_entity_ids=minted_entity_ids,
    )
    if dropped_misconceptions:
        _LOG.warning(
            "tag_mint_dropped_unlinkable_misconceptions",
            extra={
                "event": "tag_mint_dropped_unlinkable_misconceptions",
                "concept_id": concept_id,
                "dropped": dropped_misconceptions,
            },
        )
        dropped_ids = {entry["entity_id"] for entry in dropped_misconceptions}
        for key in [k for k, v in key_to_id.items() if v in dropped_ids]:
            del key_to_id[key]
        for key in [k for k, v in minted_entity_ids.items() if v in dropped_ids]:
            del minted_entity_ids[key]
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
    # of failing it. Entity minting stays fail-closed; misconception ``opposes``
    # (5a) now drops THIS mint's unlinkable rows the same way (2026-07-14),
    # while pre-existing rows keep the fail-closed contract (a mislinked entity
    # corrupts grading).
    if resolved_concept is not None:
        # Deterministic reference-graph edges: step X depends_on Y ==> the
        # apollo_entity_prereqs row (from=X, to=Y) — FROM depends on TO,
        # retaining Layer-1's legacy dependent -> prerequisite convention. This
        # is intentionally NOT the KG/canonical prerequisite -> dependent
        # convention. Replaces the LLM prereq draft. Keys are the
        # frozen prefixed canonical keys, so the resolvable-key filter,
        # concept-scope partition, and acyclicity guard below run unchanged.
        steps_by_id = {s["id"]: s for s in problem.get("reference_solution", [])}
        raw_pairs: list = [
            (_entity_key_for_step(step), _entity_key_for_step(steps_by_id[dep]))
            for step in problem.get("reference_solution", [])
            for dep in (step.get("depends_on") or [])
            if dep in steps_by_id
        ]
    else:
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
        collapsed_entity_keys=sorted(collapse_alias.keys()),
        # Drop pipeline order: unresolvable-key -> cross-concept -> cyclic.
        dropped_prereq_pairs=[*dropped_pairs, *cross_concept_pairs, *cyclic_pairs],
        concept_symbol_diagnostic=symbol_diagnostic,
    )
