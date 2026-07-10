"""Resolver V2 grounded gray-zone check (design §6, task T6).

Nodes whose fused score lands in the gray band (``t_low <= score < t_mid``,
credit 0.3 after §5.5) get ONE batched LLM call per attempt. The model must
return, per node, a VERBATIM quote from the student transcript; credit is
granted ONLY when :func:`verify_quote` confirms the quote actually appears in
the transcript (normalized substring or fuzzy-contains — the §6 hard gate,
enforced HERE regardless of what the callable claims), and is capped at
``grayzone_credit`` (0.7 — never 1.0, never the NLI-high tier).

This is a CANDIDATE GENERATOR, never an unconditional score source:

- it can only LIFT gray-band nodes to the capped credit — it never lowers
  anything and never touches non-gray nodes (only queried keys can appear in
  the returned upgrade map, and the only value ever returned is
  ``grayzone_credit``);
- at most ``max_grayzone_nodes`` nodes are queried (descending score; the
  rest keep the deterministic 0.3 gray default);
- ANY failure inside the callable (LLM/infra/JSON) is caught and the whole
  batch is a no-op (``{}``) — the grade proceeds without upgrades;
- ``fn=None`` (the ``APOLLO_RESOLVER_V2_GRAYZONE=0`` default — see
  ``config.grayzone_enabled``; the engine maps disabled -> ``None``) is a
  no-op with zero calls.

The LLM caller is an injected callable (:data:`GrayzoneFn`) so tests run with
a stub and zero network — the same injection idiom as
``apollo.grading.transcript_audit`` (``audit_fn`` / ``main_chat_auditor``).
The live implementation is :func:`main_chat_grayzone`; its client plumbing
mirrors ``main_chat_auditor`` (one ``main_chat`` call, strict JSON,
temperature 0; the OpenAI client is constructed lazily inside the call, so
this module imports without an API key).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from apollo.agent._llm import main_chat
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.types import NodeScore, RefNode

_LOG = logging.getLogger(__name__)

# §6 verification pins (design-fixed, NOT part of the T8 calibration surface).
#: Minimum normalized-quote length for the fuzzy-contains branch —
#: ``partial_ratio`` on very short strings is trivially high, so short quotes
#: must match as exact (normalized) substrings to pass.
MIN_FUZZY_QUOTE_CHARS: int = 15
#: ``rapidfuzz.fuzz.partial_ratio`` pass bar for the fuzzy-contains branch.
FUZZY_PASS_RATIO: float = 95.0

_RESPONSE_FORMAT = {"type": "json_object"}
_PURPOSE = "resolver_v2_grayzone"


@dataclass(frozen=True)
class GrayzoneQuery:
    """One gray-band node sent to the batched check (§6 prompt contract:
    ``(canonical_key, label, views)`` per node)."""

    canonical_key: str
    label: str
    views: tuple[str, ...]


@dataclass(frozen=True)
class GrayzoneVerdict:
    """The model's per-node answer. ``verified`` is set by the callable via
    :func:`verify_quote` (a ``taught=True`` with an unverifiable quote comes
    back ``verified=False`` — the §6/FINDINGS-§5 auto-NO); ``apply_grayzone``
    re-verifies independently before granting any credit."""

    canonical_key: str
    taught: bool
    quote: str | None
    verified: bool


#: The injected batched checker: ``(queries, transcript) -> verdicts``.
#: Tests inject a fake; the live impl is :func:`main_chat_grayzone`;
#: ``None`` = disabled (design §6 modes).
GrayzoneFn = Callable[[tuple[GrayzoneQuery, ...], str], tuple[GrayzoneVerdict, ...]]


def _normalize(text: str) -> str:
    """§6 normalization: casefold + collapse every whitespace run to a single
    space (so line wrapping / double spaces / case never fail a real quote)."""
    return " ".join(text.split()).casefold()


def verify_quote(quote: str, transcript: str) -> bool:
    """§6 hard gate: does ``quote`` actually appear in ``transcript``?

    Both sides are normalized (casefold, collapse whitespace). Pass iff the
    normalized quote is a substring, OR (fuzzy branch) the normalized quote is
    at least :data:`MIN_FUZZY_QUOTE_CHARS` chars and
    ``fuzz.partial_ratio >= FUZZY_PASS_RATIO`` (the design's Python-precedence
    reading: the length floor guards the fuzzy branch, where short strings
    trivially saturate ``partial_ratio``). Empty/whitespace quotes never pass.
    """
    norm_quote = _normalize(quote)
    if not norm_quote:
        return False
    norm_transcript = _normalize(transcript)
    if norm_quote in norm_transcript:
        return True
    if len(norm_quote) < MIN_FUZZY_QUOTE_CHARS:
        return False
    return fuzz.partial_ratio(norm_quote, norm_transcript) >= FUZZY_PASS_RATIO


def _query_for(node: NodeScore, ref: RefNode | None) -> GrayzoneQuery:
    """Build the prompt-contract query for one gray node. ``NodeScore`` carries
    no label/views, so they come from the optional ``ref_nodes`` mapping;
    absent, degrade to the canonical key (mirrors the views.py label-only
    degrade — never raise)."""
    if ref is not None:
        return GrayzoneQuery(
            canonical_key=node.canonical_key, label=ref.label, views=ref.views
        )
    return GrayzoneQuery(
        canonical_key=node.canonical_key,
        label=node.canonical_key,
        views=(node.canonical_key,),
    )


def apply_grayzone(
    gray: Sequence[NodeScore],
    transcript: str,
    fn: GrayzoneFn | None,
    params: ResolverV2Params,
    *,
    ref_nodes: Mapping[str, RefNode] | None = None,
) -> dict[str, float]:
    """Run the batched gray-zone check and return the upgrade map
    ``{canonical_key: params.grayzone_credit}`` for verified-YES nodes ONLY.

    Everything else stays unchanged (absent from the map -> the caller keeps
    the 0.3 gray default). Guarantees, in order:

    - ``fn is None`` (grayzone disabled) or empty ``gray`` -> ``{}`` with NO
      call;
    - at most ``params.max_grayzone_nodes`` nodes are queried, by descending
      ``score`` (tie -> canonical_key, deterministic); un-queried gray nodes
      are never upgraded;
    - exactly ONE ``fn`` call per attempt (the whole batch in one call);
    - any exception inside ``fn`` is caught -> ``{}`` for the batch (failure
      = no-op; the empty map IS the failure record — the grade proceeds);
    - only-lift: a verdict for an un-queried key is ignored (logged); an
      upgrade requires ``taught`` AND ``verified`` AND an independent
      :func:`verify_quote` pass here (belt-and-braces — a lying callable
      cannot mint credit with a fabricated quote); the ONLY value ever
      written is ``params.grayzone_credit``.

    ``ref_nodes`` (keyword-only, optional) supplies the §6 prompt labels/views
    per key; ``NodeScore`` does not carry them (fallback: canonical key).
    """
    if fn is None or not gray:
        return {}
    cap = max(0, params.max_grayzone_nodes)
    if cap == 0:
        return {}
    ordered = sorted(gray, key=lambda node: (-node.score, node.canonical_key))
    lookup: Mapping[str, RefNode] = ref_nodes or {}
    queries = tuple(
        _query_for(node, lookup.get(node.canonical_key)) for node in ordered[:cap]
    )
    try:
        verdicts = fn(queries, transcript)
    except Exception:  # noqa: BLE001 — §6: any LLM/infra failure = batch no-op
        _LOG.warning(
            "resolver_v2_grayzone_failed queried=%d (no upgrades applied)",
            len(queries),
            exc_info=True,
        )
        return {}

    asked = {query.canonical_key for query in queries}
    upgrades: dict[str, float] = {}
    for verdict in verdicts:
        if verdict.canonical_key not in asked:
            _LOG.info(
                "resolver_v2_grayzone_unasked_key key=%s", verdict.canonical_key
            )
            continue
        if not (verdict.taught and verdict.verified):
            continue
        if verdict.quote is None or not verify_quote(verdict.quote, transcript):
            continue
        upgrades[verdict.canonical_key] = params.grayzone_credit
    return upgrades


def _build_messages(
    queries: tuple[GrayzoneQuery, ...], transcript: str
) -> list[dict[str, str]]:
    """§6 prompt contract. Strict-JSON note: the OpenAI ``json_object``
    response format requires a top-level object, so the §6 verdict list rides
    under a ``"verdicts"`` wrapper key (same idiom as ``transcript_audit``'s
    ``"spans"``)."""
    node_lines: list[str] = []
    for query in queries:
        line = f"- {query.canonical_key}: {query.label}"
        extra_views = [view for view in query.views if view != query.label]
        if extra_views:
            line += " (equivalently: " + " | ".join(extra_views) + ")"
        node_lines.append(line)
    system = (
        "You check a student's teaching transcript for concepts whose automated "
        "score fell in an uncertain band. For EACH listed concept, decide "
        "whether the student actually taught it in the transcript. Return "
        'STRICT JSON {"verdicts": [{"canonical_key": "<key>", '
        '"taught": true|false, "quote": "<verbatim span>" | null}]} with '
        "exactly one entry per listed concept. When taught is true, the quote "
        "MUST be copied character-for-character from the transcript — never "
        "paraphrase, never invent text; an unverifiable quote is treated as "
        "not taught. When taught is false, quote must be null."
    )
    user = (
        "Concepts to check:\n"
        + "\n".join(node_lines)
        + f"\n\nStudent transcript:\n{transcript}\n\n"
        + 'Respond with {"verdicts": [...]} only.'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def main_chat_grayzone(
    queries: tuple[GrayzoneQuery, ...], transcript: str
) -> tuple[GrayzoneVerdict, ...]:
    """The live :data:`GrayzoneFn`: ONE ``main_chat`` call (main model,
    temperature 0, strict JSON), §6 prompt contract, then :func:`verify_quote`
    on every returned quote to set ``verified`` (a YES with an unverifiable
    quote comes back ``verified=False`` — the auto-NO).

    Returns exactly one verdict per query, in query order; a key the model
    omitted (or returned malformed) defaults to a not-taught verdict. Any
    infra/JSON-parse failure PROPAGATES — :func:`apply_grayzone` owns the
    catch-to-no-op (mirrors how ``main_chat_auditor`` surfaces rather than
    swallows; here the named-error indirection is unnecessary because the
    caller's contract is already "any exception = batch no-op")."""
    if not queries:
        return ()
    raw = main_chat(
        purpose=_PURPOSE,
        messages=_build_messages(queries, transcript),
        response_format=_RESPONSE_FORMAT,
        temperature=0.0,
    )
    parsed = json.loads(raw or "{}")
    items = parsed.get("verdicts", []) if isinstance(parsed, dict) else []
    by_key: dict[str, tuple[bool, str | None]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("canonical_key", ""))
        quote_raw = item.get("quote")
        quote = str(quote_raw) if quote_raw is not None else None
        by_key[key] = (bool(item.get("taught", False)), quote)

    verdicts: list[GrayzoneVerdict] = []
    for query in queries:
        taught, quote = by_key.get(query.canonical_key, (False, None))
        verified = bool(
            taught and quote is not None and verify_quote(quote, transcript)
        )
        verdicts.append(
            GrayzoneVerdict(
                canonical_key=query.canonical_key,
                taught=taught,
                quote=quote,
                verified=verified,
            )
        )
    return tuple(verdicts)
