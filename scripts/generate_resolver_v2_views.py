#!/usr/bin/env python
"""Offline one-time generator for the Resolver V2 multi-view cache (T2).

Design: docs/_archive/specs/2026-07-07-resolver-v2-design.md §5.2 / task card T2.

For every reference-solution step of every problem payload it can find, this
script asks the OpenAI API (ONE chat call per problem, strict JSON schema,
temperature 0) for 3-4 AFFIRMATIVE paraphrase "views" per node:

1. a definition-style restatement,
2. an application/causal-style statement,
3. a plain-language restatement,
4. (equations only) a spoken form of the formula.

Every candidate view is validated locally (affirmative — no negation markers
from ``apollo.resolution.polarity``; no hedges; <= 25 words; non-empty) and
offenders get exactly ONE regeneration round; whatever passes is kept.

Output: ``apollo/resolver_v2/views/views_cache.json`` shaped per §5.2 —

    {"_meta": {...}, "<concept_id>/<problem_id>": {"<entity_key>": [views...]}}

written with ``json.dumps(..., indent=2, sort_keys=True)`` so re-runs produce a
stable file layout. The payload step's ``content.label`` is NOT stored here —
the runtime loader (T4 ``build_ref_nodes``) always prepends it as view 0.

Problem enumeration: the committed seed bank ``apollo/subjects/**`` (10 files,
READ-only — never written) PLUS the linear_motion reference payload used by the
F1c corpus, which was auto-provisioned into the DB and has no seed dir — its
reference JSON lives with the campaign cast (see DEFAULT_PROBLEM_GLOBS).

Usage (from the repo root, with .env.campaign sourced for OPENAI_API_KEY):

    python scripts/generate_resolver_v2_views.py                # full run
    python scripts/generate_resolver_v2_views.py --only gdp_identity
    python scripts/generate_resolver_v2_views.py --dry-run      # no API, no write
    python scripts/generate_resolver_v2_views.py --validate     # gate the artifact
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Import (don't duplicate) the NLI negation lexicon — polarity.py is the single
# source of truth for it (same private-import trade-off graph_compare/canonical.py
# makes for _node_type_for_entry: importing beats drifting).
from apollo.resolution.polarity import _NEGATION  # noqa: E402

_LOG = logging.getLogger("generate_resolver_v2_views")

CACHE_PATH = REPO_ROOT / "apollo" / "resolver_v2" / "views" / "views_cache.json"

DEFAULT_PROBLEM_GLOBS: tuple[str, ...] = (
    # The 10 committed seed problems (fluid_mechanics 5, macroeconomics 5).
    "apollo/subjects/*/concepts/*/problems/*.json",
    # F1c linear_motion (kinematics_constant_acceleration/cyclist_accel_v_and_distance):
    # auto-provisioned subject, reference payload lives with the campaign cast.
    "campaign/cast/personas/linear_motion/reference/*/problems/*.json",
)

MODEL_ENV = "MAIN_MODEL"  # mirror apollo/agent/_llm.py main_chat
MODEL_DEFAULT = "gpt-4o"

MAX_VIEW_WORDS = 25
PROMPT_WORD_BUDGET = 22  # ask for less than the hard cap so validation rarely fires
MIN_VIEWS_PER_KEY = 3
MAX_VIEWS_PER_KEY = 4

# USD per 1M tokens (input, output) — for the cost report only.
_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}

_HEDGES = frozenset(
    {
        "might",
        "may",
        "could",
        "perhaps",
        "possibly",
        "probably",
        "maybe",
        "sometimes",
        "somewhat",
        "roughly",
        "arguably",
    }
)

# Extra content fields (beyond label) surfaced to the LLM, per entry_type.
_CONTENT_FIELDS = (
    "symbolic",
    "variables",
    "applies_when",
    "transformation",
    "substitution",
    "concept",
    "meaning",
    "action",
    "purpose",
    "uses_equations",
)


# ---------------------------------------------------------------------------
# View validation (the affirmative-grammar gate, §5.2 + card T2)
# ---------------------------------------------------------------------------


def _view_tokens(text: str) -> list[str]:
    return [w.strip(".,;:!?\"'()").lower() for w in text.split()]


def view_offenses(view: str) -> tuple[str, ...]:
    """Return the rule violations for a candidate view ('' clean -> empty tuple).

    Rules (card T2): non-empty; <= 25 words; AFFIRMATIVE — no token from the
    polarity negation lexicon (including litotes forms like "no change": the
    NLI-memo lesson is that ANY negation surface trips polarity screens), no
    "n't" contraction, no hedge words.
    """
    stripped = view.strip()
    if not stripped:
        return ("empty",)
    offenses: list[str] = []
    words = stripped.split()
    if len(words) > MAX_VIEW_WORDS:
        offenses.append(f"too_long({len(words)}w)")
    tokens = _view_tokens(stripped)
    negs = sorted({t for t in tokens if t in _NEGATION or t.endswith("n't")})
    if negs:
        offenses.append("negation(" + ",".join(negs) + ")")
    hedges = sorted({t for t in tokens if t in _HEDGES})
    if hedges:
        offenses.append("hedge(" + ",".join(hedges) + ")")
    return tuple(offenses)


def clean_views(
    candidates: list[str], *, label: str, keep: list[str]
) -> tuple[list[str], list[tuple[str, tuple[str, ...]]]]:
    """Validate + dedup candidate views against already-kept ones.

    Returns (kept_new, rejected) where rejected pairs each dropped view with its
    offenses. Views duplicating the label or an earlier view (case-insensitive)
    are silently dropped (the loader prepends the label as view 0).
    """
    kept_new: list[str] = []
    rejected: list[tuple[str, tuple[str, ...]]] = []
    seen = {label.strip().lower()} | {v.strip().lower() for v in keep}
    for cand in candidates:
        if not isinstance(cand, str):
            rejected.append((repr(cand), ("non_string",)))
            continue
        norm = cand.strip().lower()
        if norm in seen:
            continue
        offenses = view_offenses(cand)
        if offenses:
            rejected.append((cand, offenses))
            continue
        seen.add(norm)
        kept_new.append(cand.strip())
        if len(keep) + len(kept_new) >= MAX_VIEWS_PER_KEY:
            break
    return kept_new, rejected


# ---------------------------------------------------------------------------
# Problem enumeration + payload reading (READ-only)
# ---------------------------------------------------------------------------


def enumerate_problem_files(extra_roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in DEFAULT_PROBLEM_GLOBS:
        files.extend(REPO_ROOT.glob(pattern))
    for root in extra_roots:
        files.extend(Path(root).glob("**/problems/*.json"))
    return sorted(set(files))


def read_problem(path: Path) -> dict[str, Any]:
    """Read one problem payload into the generator's working shape."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    concept_id = payload.get("concept_id")
    problem_id = payload.get("id")
    if not concept_id or not problem_id:
        raise ValueError(f"{path}: payload missing concept_id/id")
    steps = payload.get("reference_solution") or []
    nodes: list[dict[str, Any]] = []
    for step in steps:
        key = step.get("entity_key")
        content = step.get("content") or {}
        label = content.get("label")
        if not key:
            raise ValueError(f"{path}: reference step missing entity_key: {step!r}")
        node: dict[str, Any] = {
            "entity_key": key,
            "entry_type": step.get("entry_type"),
            "label": label or key,
        }
        for field in _CONTENT_FIELDS:
            if field in content and content[field] is not None:
                node[field] = content[field]
        nodes.append(node)
    if not nodes:
        raise ValueError(f"{path}: empty reference_solution")
    return {
        "pair": f"{concept_id}/{problem_id}",
        "concept_id": concept_id,
        "problem_id": problem_id,
        "problem_text": payload.get("problem_text") or "",
        "target_unknown": payload.get("target_unknown"),
        "nodes": nodes,
        "source_file": path.relative_to(REPO_ROOT).as_posix()
        if path.is_relative_to(REPO_ROOT)
        else str(path),
    }


# ---------------------------------------------------------------------------
# OpenAI call (mirrors apollo/agent/_llm.py main_chat, but returns usage so the
# script can report a token/cost estimate)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = f"""You write paraphrase "views" for the reference nodes of a teaching problem.
Each view is an NLI entailment HYPOTHESIS: a statement that is true and would be
entailed by a student's transcript whenever the student correctly taught that
node's content.

HARD requirements for EVERY view:
- ONE simple affirmative declarative sentence, present tense.
- ABSOLUTELY NO negation words (not, no, never, cannot, without, neither, nor,
  any n't contraction) and NO litotes: instead of "does not change" or
  "no change", write "stays constant" or "remains the same".
- NO hedging words (might, may, could, perhaps, possibly, probably, maybe).
- At most {PROMPT_WORD_BUDGET} words.
- Self-contained: name the quantities explicitly (say "the fluid pressure",
  never "it" or "this value").
- State the concept content itself. Never mention "the student", "the problem",
  "this step", or "the equation above".
- Avoid the problem's specific numeric values; qualitative wording is fine.

For each node produce, in this order:
1. a definition-style restatement of the node's content;
2. an application/causal-style statement (what it is used for or what it implies);
3. a plain-language restatement in everyday words.
For nodes with entry_type "equation" ALSO produce:
4. a spoken form of the formula in words (e.g. "the product of area and
   velocity is the same at both sections").

Return JSON only: one array of view strings per entity key."""


def _resolve_model() -> str:
    return os.getenv(MODEL_ENV) or MODEL_DEFAULT


def _build_schema(entity_keys: list[str]) -> dict[str, Any]:
    """Strict JSON-schema response_format keyed by the problem's entity keys."""
    properties = {
        key: {
            "type": "array",
            "items": {"type": "string"},
            "description": f"Paraphrase views for reference node {key}",
        }
        for key in entity_keys
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "resolver_v2_views",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": properties,
                "required": list(entity_keys),
                "additionalProperties": False,
            },
        },
    }


def _chat_json(
    *,
    purpose: str,
    messages: list[dict[str, Any]],
    response_format: dict[str, Any],
    usage_totals: dict[str, int],
) -> dict[str, Any]:
    """One strict-JSON chat call; mirrors apollo/agent/_llm.py main_chat but
    also accumulates token usage into ``usage_totals`` for the cost report."""
    from openai import OpenAI  # lazy: --dry-run/--validate need no key/client

    model = _resolve_model()
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format=response_format,
        temperature=0.0,
    )
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    usage_totals["prompt_tokens"] = usage_totals.get("prompt_tokens", 0) + tokens_in
    usage_totals["completion_tokens"] = usage_totals.get("completion_tokens", 0) + tokens_out
    usage_totals["calls"] = usage_totals.get("calls", 0) + 1
    _LOG.info(
        "llm_call",
        extra={"event": "llm_call", "purpose": purpose, "model": model,
               "tokens_in": tokens_in, "tokens_out": tokens_out},
    )
    print(f"  [llm] {purpose}: model={model} tokens_in={tokens_in} tokens_out={tokens_out}")
    content = resp.choices[0].message.content or ""
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError(f"{purpose}: model returned non-object JSON")
    return parsed


# ---------------------------------------------------------------------------
# Per-problem generation (one call + at most one regeneration round)
# ---------------------------------------------------------------------------


def generate_for_problem(
    problem: dict[str, Any], usage_totals: dict[str, int]
) -> tuple[dict[str, list[str]], list[str]]:
    """Generate validated views for one problem.

    Returns (views_by_key, warnings). One initial call; keys left with fewer
    than MIN_VIEWS_PER_KEY valid views after validation get exactly ONE
    regeneration round; whatever passes is kept (card T2: do not retry forever).
    """
    keys = [n["entity_key"] for n in problem["nodes"]]
    labels = {n["entity_key"]: n["label"] for n in problem["nodes"]}
    user_payload = {
        "concept_id": problem["concept_id"],
        "problem_id": problem["problem_id"],
        "problem_text": problem["problem_text"],
        "target_unknown": problem["target_unknown"],
        "reference_nodes": problem["nodes"],
    }
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Generate views for every reference node of this problem:\n"
            + json.dumps(user_payload, indent=2, sort_keys=True),
        },
    ]
    raw = _chat_json(
        purpose=f"resolver_v2_views:{problem['problem_id']}",
        messages=messages,
        response_format=_build_schema(keys),
        usage_totals=usage_totals,
    )

    views: dict[str, list[str]] = {}
    rejected_by_key: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for key in keys:
        candidates = raw.get(key) or []
        kept, rejected = clean_views(list(candidates), label=labels[key], keep=[])
        views[key] = kept
        if rejected:
            rejected_by_key[key] = rejected

    warnings: list[str] = []
    retry_keys = [k for k in keys if len(views[k]) < MIN_VIEWS_PER_KEY]
    if retry_keys:
        reject_report = {
            k: [{"view": v, "offenses": list(off)} for v, off in rejected_by_key.get(k, [])]
            for k in retry_keys
        }
        need = {k: MIN_VIEWS_PER_KEY - len(views[k]) for k in retry_keys}
        retry_messages = messages + [
            {"role": "assistant", "content": json.dumps(raw, sort_keys=True)},
            {
                "role": "user",
                "content": (
                    "Some views violated the hard requirements and were dropped. "
                    "Produce REPLACEMENT views for ONLY these entity keys "
                    "(strictly affirmative, no negation words or litotes, no hedges, "
                    f"<= {PROMPT_WORD_BUDGET} words). Needed replacements per key: "
                    + json.dumps(need, sort_keys=True)
                    + "\nDropped views and the rule each broke:\n"
                    + json.dumps(reject_report, indent=2, sort_keys=True)
                ),
            },
        ]
        raw_retry = _chat_json(
            purpose=f"resolver_v2_views_retry:{problem['problem_id']}",
            messages=retry_messages,
            response_format=_build_schema(retry_keys),
            usage_totals=usage_totals,
        )
        for key in retry_keys:
            kept, _ = clean_views(
                list(raw_retry.get(key) or []), label=labels[key], keep=views[key]
            )
            views[key].extend(kept)

    for key in keys:
        if not views[key]:
            warnings.append(
                f"{problem['pair']}::{key}: 0 valid views after retry "
                "(runtime degrades to label-only)"
            )
        elif len(views[key]) < MIN_VIEWS_PER_KEY:
            warnings.append(
                f"{problem['pair']}::{key}: only {len(views[key])} valid views "
                f"(target {MIN_VIEWS_PER_KEY})"
            )
    return views, warnings


# ---------------------------------------------------------------------------
# Cache IO + validation gate
# ---------------------------------------------------------------------------


def load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def write_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def validate_cache(
    cache: dict[str, Any], problems: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """The card-T2 artifact gate.

    Checks: every enumerated problem covered; every reference entity_key present
    with a non-empty list of strings; every view passes the affirmative gate
    (in particular no ' not ' / 'never' / \"n't\"). Returns (errors, summary_rows).
    """
    errors: list[str] = []
    rows: list[str] = []
    for problem in problems:
        pair = problem["pair"]
        entry = cache.get(pair)
        if not isinstance(entry, dict):
            errors.append(f"missing problem entry: {pair}")
            continue
        counts: list[int] = []
        for node in problem["nodes"]:
            key = node["entity_key"]
            views = entry.get(key)
            if not isinstance(views, list) or not views:
                errors.append(f"{pair}::{key}: missing or empty view list")
                counts.append(0)
                continue
            counts.append(len(views))
            for view in views:
                if not isinstance(view, str):
                    errors.append(f"{pair}::{key}: non-string view {view!r}")
                    continue
                offenses = view_offenses(view)
                if offenses:
                    errors.append(f"{pair}::{key}: {','.join(offenses)}: {view!r}")
        rows.append(
            f"{pair}: {len(problem['nodes'])} nodes, views/node "
            f"min={min(counts) if counts else 0} max={max(counts) if counts else 0} "
            f"total={sum(counts)}"
        )
    return errors, rows


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="regenerate only this problem_id (payload id or file stem); repeatable",
    )
    parser.add_argument("--dry-run", action="store_true", help="enumerate + plan, no API, no write")
    parser.add_argument(
        "--validate", action="store_true", help="validate the committed cache and exit"
    )
    parser.add_argument(
        "--extra-root",
        action="append",
        default=[],
        help="additional directory scanned for **/problems/*.json",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    files = enumerate_problem_files(args.extra_root)
    problems: list[dict[str, Any]] = []
    for path in files:
        try:
            problems.append(read_problem(path))
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            print(f"ERROR reading {path}: {exc}")
            return 1
    pairs = [p["pair"] for p in problems]
    if len(set(pairs)) != len(pairs):
        dupes = sorted({p for p in pairs if pairs.count(p) > 1})
        print(f"ERROR: duplicate concept_id/problem_id pairs across files: {dupes}")
        return 1
    print(f"Found {len(problems)} problem payloads:")
    for problem in problems:
        print(f"  {problem['pair']}  ({len(problem['nodes'])} nodes)  <- {problem['source_file']}")

    if args.validate:
        cache = load_cache()
        if not cache:
            print(f"ERROR: cache missing or empty at {CACHE_PATH}")
            return 1
        errors, rows = validate_cache(cache, problems)
        print("\nCoverage summary:")
        for row in rows:
            print("  " + row)
        if errors:
            print(f"\nVALIDATION FAILED ({len(errors)} errors):")
            for err in errors:
                print("  " + err)
            return 1
        print("\nVALIDATION PASSED")
        return 0

    if args.only:
        wanted = set(args.only)
        problems = [
            p
            for p in problems
            if p["problem_id"] in wanted or Path(p["source_file"]).stem in wanted
        ]
        if not problems:
            print(f"ERROR: --only matched no problems: {sorted(wanted)}")
            return 1

    if args.dry_run:
        total_nodes = sum(len(p["nodes"]) for p in problems)
        print(
            f"\nDRY RUN: would make {len(problems)}(+retries) OpenAI calls "
            f"covering {total_nodes} reference nodes; cache -> {CACHE_PATH}"
        )
        return 0

    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set (source .env.campaign first)")
        return 1

    cache = load_cache()
    meta = cache.get("_meta") if isinstance(cache.get("_meta"), dict) else {}
    source_labels = meta.get("source_labels") if isinstance(meta.get("source_labels"), dict) else {}
    source_files = meta.get("source_files") if isinstance(meta.get("source_files"), dict) else {}

    usage_totals: dict[str, int] = {}
    all_warnings: list[str] = []
    for problem in problems:
        print(f"\nGenerating views for {problem['pair']} ...")
        views, warnings = generate_for_problem(problem, usage_totals)
        cache[problem["pair"]] = {k: list(v) for k, v in views.items()}
        source_labels[problem["pair"]] = {
            n["entity_key"]: n["label"] for n in problem["nodes"]
        }
        source_files[problem["pair"]] = problem["source_file"]
        all_warnings.extend(warnings)

    cache["_meta"] = {
        "date": _dt.date.today().isoformat(),
        "generator": "scripts/generate_resolver_v2_views.py",
        "model": _resolve_model(),
        "rules": "affirmative declarative, no negation/litotes/hedges, <=25 words; "
        "label is NOT stored (loader prepends it as view 0)",
        "source_files": source_files,
        "source_labels": source_labels,
    }
    write_cache(cache)
    print(f"\nWrote {CACHE_PATH} ({CACHE_PATH.stat().st_size} bytes)")

    # Re-validate the artifact we just wrote (the card gate), full problem set.
    full_problems = [read_problem(p) for p in enumerate_problem_files(args.extra_root)]
    errors, rows = validate_cache(load_cache(), full_problems)
    print("\nCoverage summary:")
    for row in rows:
        print("  " + row)
    if all_warnings:
        print("\nWarnings:")
        for warning in all_warnings:
            print("  " + warning)

    tokens_in = usage_totals.get("prompt_tokens", 0)
    tokens_out = usage_totals.get("completion_tokens", 0)
    model = _resolve_model()
    price = _PRICE_PER_M.get(model)
    cost = (
        f"~${tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1]:.4f}"
        if price
        else "unknown model pricing"
    )
    print(
        f"\nOpenAI usage: {usage_totals.get('calls', 0)} calls, "
        f"{tokens_in} prompt + {tokens_out} completion tokens ({model}, est. {cost})"
    )

    if errors:
        print(f"\nVALIDATION FAILED ({len(errors)} errors):")
        for err in errors:
            print("  " + err)
        return 1
    print("\nVALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
