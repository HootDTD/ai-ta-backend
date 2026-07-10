"""Resolver V2 lexical prefilter — the runtime NLI guard (§5.3, T3).

Pure lexical scorer + deterministic top-K window selection. NO embedder, NO
small-NLI fallback (design §5.3): the score is

    lex(window, view) = 0.5 * token_set_ratio(window, view) / 100
                      + 0.5 * |content(window) ∩ content(view)|
                            / max(1, |content(view)|)

``content(t)`` mirrors ``apollo.resolution.nli_resolution._content_tokens``
(lowercased whitespace tokens, raw length > 2, stripped of ``.,;:!?``).
``token_set_ratio`` inputs are lowercased so casing never affects ranking
(the overlap term is already case-blind).

``select_windows`` satisfies the T1 ``SelectFn`` contract — T4 (node
scoring) and T5 (edges) receive it as an injected callable and never import
this module. Deterministic and free: same inputs -> same output, ties break
to the lowest window index.

Imports: stdlib + rapidfuzz (existing pinned dep) + the T1 types only.
"""

from __future__ import annotations

from collections.abc import Sequence

from rapidfuzz import fuzz

from apollo.resolver_v2.types import Window


def _content_tokens(text: str) -> frozenset[str]:
    """Mirror of ``apollo.resolution.nli_resolution._content_tokens``:
    lowercased tokens with raw length > 2, stripped of ``.,;:!?``."""
    return frozenset(w.strip(".,;:!?").lower() for w in text.split() if len(w) > 2)


def lexical_score(window_text: str, view_text: str) -> float:
    """§5.3 lexical similarity in ``[0, 1]``: equal-weight blend of
    order-insensitive ``token_set_ratio`` (repo fuzzy-tier convention,
    normalized to 0..1) and content-token recall of the view. Empty or
    fully-disjoint inputs score 0.0."""
    fuzzy = fuzz.token_set_ratio(window_text.lower(), view_text.lower()) / 100.0
    view_tokens = _content_tokens(view_text)
    overlap = len(_content_tokens(window_text) & view_tokens) / max(1, len(view_tokens))
    return 0.5 * fuzzy + 0.5 * overlap


def select_windows(
    windows: Sequence[Window], view_text: str, k: int
) -> tuple[tuple[int, float], ...]:
    """Top-``k`` windows for one view, per the ``SelectFn`` contract:
    ``((window.index, lex), ...)`` sorted by ``(-lex, window.index)`` —
    highest score first, ties to the lowest index. ``k <= 0`` or no windows
    -> ``()``. Pure and deterministic."""
    if k <= 0 or not windows:
        return ()
    scored = ((window.index, lexical_score(window.text, view_text)) for window in windows)
    ranked = sorted(scored, key=lambda pair: (-pair[1], pair[0]))
    return tuple(ranked[:k])
