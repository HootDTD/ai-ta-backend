"""T6 smoke tests: grounded gray-zone check (design §6).

Smoke tier per the design card (quote-verification accepts/rejects, cap
enforcement, only-lift invariant, batch cap, failure = no-op, disabled = no-op);
the 95% patch gate is explicitly deferred for this branch. Every test injects a
fake :data:`GrayzoneFn` or patches ``grayzone.main_chat`` — zero network, zero
model loads.
"""

from __future__ import annotations

import json

import apollo.resolver_v2.grayzone as grayzone
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.grayzone import (
    GrayzoneQuery,
    GrayzoneVerdict,
    apply_grayzone,
    main_chat_grayzone,
    verify_quote,
)
from apollo.resolver_v2.types import NodeScore, RefNode

_PARAMS = ResolverV2Params()  # defaults: max_grayzone_nodes=8, grayzone_credit=0.7

_TRANSCRIPT = (
    "Student: The flow rate stays constant along the pipe.\n"
    "Student: Also, pressure drops where velocity rises — that is Bernoulli.\n"
    "Student: I think area times velocity is the same at both sections."
)

_REAL_QUOTE = "pressure drops where velocity rises"
_FAKE_QUOTE = "energy is always conserved in every pipe"


def _gray(key: str, score: float = 0.5) -> NodeScore:
    """A gray-band NodeScore (t_low 0.40 <= score < t_mid 0.75, credit 0.3)."""
    return NodeScore(canonical_key=key, score=score, credit=0.3, source="nli", best=None)


def _verdict(key: str, *, taught: bool = True, quote: str | None = _REAL_QUOTE,
             verified: bool = True) -> GrayzoneVerdict:
    return GrayzoneVerdict(canonical_key=key, taught=taught, quote=quote, verified=verified)


class FakeGrayzone:
    """Recording fake GrayzoneFn: returns pre-scripted verdicts per key
    (default: not taught), or raises when constructed with ``error``."""

    def __init__(self, verdicts_by_key=None, error: Exception | None = None):
        self.verdicts_by_key = dict(verdicts_by_key or {})
        self.error = error
        self.calls: list[tuple[tuple[GrayzoneQuery, ...], str]] = []
        self.extra_verdicts: list[GrayzoneVerdict] = []

    def __call__(self, queries, transcript):
        self.calls.append((queries, transcript))
        if self.error is not None:
            raise self.error
        out = [
            self.verdicts_by_key.get(
                q.canonical_key, _verdict(q.canonical_key, taught=False, quote=None, verified=False)
            )
            for q in queries
        ]
        return tuple(out + self.extra_verdicts)


# --- verify_quote: accepts ------------------------------------------------------


def test_verify_quote_exact_passes():
    assert verify_quote(_REAL_QUOTE, _TRANSCRIPT) is True


def test_verify_quote_whitespace_case_mangled_passes():
    """§6 normalization: casefold + collapsed whitespace still verifies."""
    mangled = "  PRESSURE   drops\nwhere Velocity\t rises "
    assert verify_quote(mangled, _TRANSCRIPT) is True


def test_verify_quote_fuzzy_near_match_passes():
    """Fuzzy branch: one-char typo, len >= 15 -> partial_ratio >= 95 passes."""
    typo = "pressure drops where velocty rises"  # 'velocity' -> 'velocty'
    assert typo not in _TRANSCRIPT
    assert verify_quote(typo, _TRANSCRIPT) is True


# --- verify_quote: rejects ------------------------------------------------------


def test_verify_quote_fabricated_fails():
    assert verify_quote(_FAKE_QUOTE, _TRANSCRIPT) is False


def test_verify_quote_short_fuzzy_fails():
    """A short (<15 chars normalized) non-substring never passes via fuzz."""
    assert verify_quote("velocty rises", _TRANSCRIPT) is False


def test_verify_quote_empty_fails():
    assert verify_quote("", _TRANSCRIPT) is False
    assert verify_quote("   \n\t", _TRANSCRIPT) is False


# --- apply_grayzone: happy path --------------------------------------------------


def test_apply_grayzone_upgrades_only_verified_yes():
    """Verified YES lifts exactly that key to grayzone_credit; the not-taught
    node is untouched (absent from the map -> stays at the 0.3 gray default)."""
    fn = FakeGrayzone({"node.a": _verdict("node.a")})
    upgrades = apply_grayzone([_gray("node.a"), _gray("node.b")], _TRANSCRIPT, fn, _PARAMS)
    assert upgrades == {"node.a": _PARAMS.grayzone_credit}


def test_apply_grayzone_single_batched_call():
    """Exactly ONE fn call per attempt, all gray nodes batched into it."""
    fn = FakeGrayzone()
    apply_grayzone([_gray(f"node.{i}") for i in range(5)], _TRANSCRIPT, fn, _PARAMS)
    assert len(fn.calls) == 1
    queries, transcript = fn.calls[0]
    assert len(queries) == 5
    assert transcript == _TRANSCRIPT


def test_apply_grayzone_ref_nodes_enrich_queries():
    """ref_nodes supplies the §6 label/views; a missing key degrades to the
    canonical key (never raises)."""
    ref = RefNode(
        canonical_key="node.a", node_type="EQUATION",
        label="Continuity equation", views=("Continuity equation", "A1 v1 = A2 v2"),
    )
    fn = FakeGrayzone()
    apply_grayzone(
        [_gray("node.a", 0.6), _gray("node.b", 0.5)], _TRANSCRIPT, fn, _PARAMS,
        ref_nodes={"node.a": ref},
    )
    (queries, _), = fn.calls
    assert queries[0] == GrayzoneQuery(
        canonical_key="node.a", label="Continuity equation",
        views=("Continuity equation", "A1 v1 = A2 v2"),
    )
    assert queries[1] == GrayzoneQuery(
        canonical_key="node.b", label="node.b", views=("node.b",)
    )


# --- apply_grayzone: cap enforcement ---------------------------------------------


def test_apply_grayzone_caps_at_max_grayzone_nodes_by_descending_score():
    """> max_grayzone_nodes gray nodes -> only the top-8 by score are queried;
    the rest stay at the gray default (never upgraded, never sent)."""
    nodes = [_gray(f"node.{i:02d}", score=0.40 + i * 0.01) for i in range(10)]
    fn = FakeGrayzone({n.canonical_key: _verdict(n.canonical_key) for n in nodes})
    upgrades = apply_grayzone(nodes, _TRANSCRIPT, fn, _PARAMS)
    (queries, _), = fn.calls
    queried = [q.canonical_key for q in queries]
    assert len(queried) == _PARAMS.max_grayzone_nodes == 8
    # top-8 by descending score = node.09 .. node.02; the two lowest never sent
    assert queried == [f"node.{i:02d}" for i in range(9, 1, -1)]
    assert "node.00" not in upgrades and "node.01" not in upgrades
    assert set(upgrades) == set(queried)


# --- apply_grayzone: only-lift invariant -----------------------------------------


def test_apply_grayzone_fabricated_quote_never_credits():
    """Card edge: fabricated quote -> verified=False -> no upgrade."""
    fn = FakeGrayzone(
        {"node.a": _verdict("node.a", quote=_FAKE_QUOTE, verified=False)}
    )
    assert apply_grayzone([_gray("node.a")], _TRANSCRIPT, fn, _PARAMS) == {}


def test_apply_grayzone_lying_fn_cannot_mint_credit():
    """Belt-and-braces: even a verdict claiming verified=True is re-verified
    here — a fabricated quote or a None quote grants nothing, an unasked key
    is ignored, and no value other than grayzone_credit is ever emitted."""
    fn = FakeGrayzone(
        {
            "node.a": _verdict("node.a", quote=_FAKE_QUOTE, verified=True),
            "node.b": _verdict("node.b", quote=None, verified=True),
            "node.c": _verdict("node.c", taught=False, verified=True),
            "node.d": _verdict("node.d"),  # the one legitimate upgrade
        }
    )
    fn.extra_verdicts.append(_verdict("node.NOT_GRAY"))  # unasked key
    gray = [_gray(k) for k in ("node.a", "node.b", "node.c", "node.d")]
    upgrades = apply_grayzone(gray, _TRANSCRIPT, fn, _PARAMS)
    assert upgrades == {"node.d": _PARAMS.grayzone_credit}
    assert set(upgrades) <= {n.canonical_key for n in gray}  # never non-gray keys
    assert all(v == _PARAMS.grayzone_credit for v in upgrades.values())  # only-lift cap


# --- apply_grayzone: failure = no-op ---------------------------------------------


def test_apply_grayzone_fn_raising_is_caught_noop():
    """Any LLM/infra failure inside fn -> {} for the whole batch; the grade
    proceeds without upgrades (the empty map is the failure record)."""
    fn = FakeGrayzone(error=RuntimeError("llm down"))
    assert apply_grayzone([_gray("node.a")], _TRANSCRIPT, fn, _PARAMS) == {}
    assert len(fn.calls) == 1


def test_apply_grayzone_malformed_json_via_live_fn_is_noop(monkeypatch):
    """End-to-end failure path: main_chat_grayzone raises on malformed JSON
    and apply_grayzone converts it to the batch no-op."""
    monkeypatch.setattr(grayzone, "main_chat", lambda **kwargs: "not json at all")
    upgrades = apply_grayzone(
        [_gray("node.a")], _TRANSCRIPT, main_chat_grayzone, _PARAMS
    )
    assert upgrades == {}


# --- apply_grayzone: disabled / empty = no-op ------------------------------------


def test_apply_grayzone_fn_none_is_disabled_noop():
    """fn=None (APOLLO_RESOLVER_V2_GRAYZONE=0 -> the engine passes None):
    no call, no upgrades, all gray nodes keep the deterministic 0.3 default."""
    assert apply_grayzone([_gray("node.a")], _TRANSCRIPT, None, _PARAMS) == {}


def test_apply_grayzone_empty_gray_makes_no_call():
    fn = FakeGrayzone()
    assert apply_grayzone([], _TRANSCRIPT, fn, _PARAMS) == {}
    assert fn.calls == []


# --- main_chat_grayzone (patched main_chat, zero network) -------------------------


def test_main_chat_grayzone_parses_and_self_verifies(monkeypatch):
    """ONE strict-JSON call; real quote -> verified=True, fabricated quote ->
    verified=False (auto-NO), omitted key -> default not-taught verdict, all
    in query order."""
    calls: list[dict] = []

    def fake_main_chat(**kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "verdicts": [
                    {"canonical_key": "node.a", "taught": True, "quote": _REAL_QUOTE},
                    {"canonical_key": "node.b", "taught": True, "quote": _FAKE_QUOTE},
                ]
            }
        )

    monkeypatch.setattr(grayzone, "main_chat", fake_main_chat)
    queries = tuple(
        GrayzoneQuery(canonical_key=k, label=k, views=(k,))
        for k in ("node.a", "node.b", "node.c")
    )
    verdicts = main_chat_grayzone(queries, _TRANSCRIPT)
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["temperature"] == 0.0
    assert verdicts == (
        GrayzoneVerdict("node.a", taught=True, quote=_REAL_QUOTE, verified=True),
        GrayzoneVerdict("node.b", taught=True, quote=_FAKE_QUOTE, verified=False),
        GrayzoneVerdict("node.c", taught=False, quote=None, verified=False),
    )


def test_main_chat_grayzone_empty_queries_makes_no_call(monkeypatch):
    def boom(**kwargs):  # pragma: no cover - must never fire
        raise AssertionError("main_chat called for an empty batch")

    monkeypatch.setattr(grayzone, "main_chat", boom)
    assert main_chat_grayzone((), _TRANSCRIPT) == ()
