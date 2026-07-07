"""T3 smoke tests: windowing (§5.1) + lexical prefilter (§5.3).

Smoke tier per the design card (happy path + the named edge cases —
deterministic, pure, no network, no model): the 95% patch gate is explicitly
deferred for this branch.
"""

from __future__ import annotations

from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.prefilter import lexical_score, select_windows
from apollo.resolver_v2.types import SelectFn, Window
from apollo.resolver_v2.windows import build_windows, split_sentences

_PARAMS = ResolverV2Params()  # frozen design defaults: 3 sentences, 1 overlap, 120 words

_TURNS = (
    # 5 sentences -> sliding windows [s0 s1 s2], [s2 s3 s4]
    "Water flows through the pipe. The area gets smaller downstream. "
    "Continuity means flow rate is conserved. So the velocity must increase. "
    "Mass is conserved along the pipe.",
    # 3 sentences -> one window
    "Bernoulli relates pressure and speed. Higher speed means lower pressure. "
    "Energy per volume is conserved.",
)


# --- windows: happy path -------------------------------------------------------


def test_build_windows_happy_count_overlap_and_indices():
    windows = build_windows(_TURNS, _PARAMS)

    assert len(windows) == 3
    assert [w.index for w in windows] == [0, 1, 2]
    assert [w.turn_index for w in windows] == [0, 0, 1]
    # 1-sentence overlap: s2 ends window 0 AND starts window 1.
    shared = "Continuity means flow rate is conserved."
    assert windows[0].text.endswith(shared)
    assert windows[1].text.startswith(shared)
    assert windows[2].text.startswith("Bernoulli relates pressure and speed.")


def test_build_windows_deterministic_across_calls():
    # DONE criterion: same turns -> same windows (tuple equality).
    assert build_windows(_TURNS, _PARAMS) == build_windows(_TURNS, _PARAMS)


def test_split_sentences_latex_safe():
    # Periods inside $...$, $$...$$ and \[...\] never split (design §5.1).
    assert split_sentences("We use $v = 2.5$ m/s here. Next sentence.") == [
        "We use $v = 2.5$ m/s here.",
        "Next sentence.",
    ]
    assert split_sentences("Consider \\[x = 1.5\\] now. Done.") == [
        "Consider \\[x = 1.5\\] now.",
        "Done.",
    ]
    assert split_sentences("Display $$y = 3.14$$ math. End.") == [
        "Display $$y = 3.14$$ math.",
        "End.",
    ]
    # Newlines split too (outside math).
    assert split_sentences("first line\nsecond line") == ["first line", "second line"]


# --- windows: edge cases -------------------------------------------------------


def test_build_windows_empty_turns():
    assert build_windows((), _PARAMS) == ()
    assert build_windows(("", "   \n"), _PARAMS) == ()


def test_overlong_sentence_becomes_single_truncated_window():
    # One 150-word sentence (> max_window_words=120) -> exactly one window,
    # truncated at the cap.
    long_sentence = " ".join(f"w{i}" for i in range(150)) + "."
    windows = build_windows((long_sentence,), _PARAMS)

    assert len(windows) == 1
    words = windows[0].text.split()
    assert len(words) == _PARAMS.max_window_words
    assert words[0] == "w0"
    assert words[-1] == "w119"


# --- prefilter: happy path -----------------------------------------------------


def test_select_windows_ranks_on_topic_window_first():
    windows = build_windows(_TURNS, _PARAMS)
    view = "Bernoulli says higher speed means lower pressure."

    top = select_windows(windows, view, 2)

    assert len(top) == 2
    assert top[0][0] == 2  # the Bernoulli window wins
    assert top[0][1] > top[1][1]  # strictly better than the runner-up
    # SelectFn contract shape: ((window_index, lexical_score), ...).
    _typed: SelectFn = select_windows  # static contract check (no-op at runtime)
    assert all(
        isinstance(idx, int) and isinstance(score, float) for idx, score in top
    )


def test_lexical_score_identity_is_one():
    assert lexical_score("continuity equation", "continuity equation") == 1.0


# --- prefilter: edge cases -----------------------------------------------------


def test_zero_overlap_view_scores_zero_and_orders_deterministically():
    # Character-disjoint texts: token_set_ratio 0 AND content overlap 0.
    # (Single tokens: with multi-token strings token_set_ratio credits the
    # shared SPACE characters, so fully-disjoint words still score ~0.18 on
    # the fuzzy term — that residue is what lex_floor=0.10 guards.)
    windows = (
        Window(index=0, turn_index=0, text="aaabbbccc"),
        Window(index=1, turn_index=0, text="aaabbbccc"),
    )

    result = select_windows(windows, "zzzqqqxxx", 3)

    # Both score exactly 0.0; ties break to the LOWEST window index.
    assert result == ((0, 0.0), (1, 0.0))


def test_select_windows_empty_inputs():
    assert select_windows((), "anything", 3) == ()
    windows = (Window(index=0, turn_index=0, text="some text"),)
    assert select_windows(windows, "some text", 0) == ()
