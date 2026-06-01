"""P3.11 — Diagnostic narration line for negotiated entries.

The line is appended deterministically by `_append_negotiation_line`,
sourced from `coverage["negotiation_counts"]` (P3.4). It must:
    - Be empty when nothing was negotiated.
    - Use singular "entry" for total=1, plural "entries" otherwise.
    - List only the non-zero categories — no zero-padding.
    - Never name a specific entry (no leak of bank_code / surface form).

Tests cover the appender directly + an integration through
`generate_diagnostic` with a stubbed LLM.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from apollo.overseer.diagnostic import (
    _append_negotiation_line,
    generate_diagnostic,
)


def _coverage(*, paraphrased=0, skipped=0, disputed=0):
    return {
        "negotiation_counts": {
            "paraphrased": paraphrased,
            "skipped": skipped,
            "disputed": disputed,
            # `dual` is paraphrased + skipped — not separately needed by
            # the appender; included here for parity with P3.4 output.
            "dual": paraphrased + skipped,
        },
    }


def test_no_line_when_zero_negotiations():
    cov = _coverage()
    assert _append_negotiation_line("done", cov) == "done"


def test_single_paraphrase_singular_entry():
    cov = _coverage(paraphrased=1)
    out = _append_negotiation_line("base", cov)
    assert "1 entry with Apollo: 1 paraphrased" in out
    # Does NOT include zero categories.
    assert "skipped" not in out
    assert "disputed" not in out


def test_three_kinds_appended_in_canonical_order():
    cov = _coverage(paraphrased=2, skipped=1, disputed=3)
    out = _append_negotiation_line("base", cov)
    assert "6 entries with Apollo:" in out
    # Canonical order: paraphrased, skipped, disputed.
    p_pos = out.find("paraphrased")
    s_pos = out.find("skipped")
    d_pos = out.find("disputed")
    assert p_pos < s_pos < d_pos


def test_only_skipped_lists_only_skipped():
    cov = _coverage(skipped=4)
    out = _append_negotiation_line("base", cov)
    assert "4 entries with Apollo: 4 skipped." in out
    assert "paraphrased" not in out
    assert "disputed" not in out


def test_legacy_coverage_no_negotiation_counts_is_noop():
    """Pre-P3.4 coverage dicts have no `negotiation_counts` — appender
    must treat counts as zero, narrative untouched."""
    legacy = {"per_step": {}}
    assert _append_negotiation_line("legacy", legacy) == "legacy"


def test_line_does_not_name_entries():
    """Defense-in-depth: even if a future caller adds `entries` to
    negotiation_counts, the appender's deterministic phrasing never
    references them."""
    cov = _coverage(paraphrased=1)
    cov["negotiation_counts"]["entries"] = ["secret-entry-id"]
    out = _append_negotiation_line("base", cov)
    assert "secret-entry-id" not in out


# ---------------------------------------------------------------------------
# Integration with generate_diagnostic (LLM stubbed)
# ---------------------------------------------------------------------------

def _mock_reply(text: str):
    return MagicMock(choices=[MagicMock(message=MagicMock(content=text))])


@patch("apollo.overseer.diagnostic.OpenAI")
def test_generate_diagnostic_appends_negotiation_line_when_present(mock_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("Solid teach.")
    mock_cls.return_value = client

    out = generate_diagnostic(
        coverage=_coverage(paraphrased=2, disputed=1),
        solver_result={"status": "solved"},
        reference_steps=[],
        problem_text="x",
        rubric={"overall": {"score": 80}},
    )
    assert out.startswith("Solid teach.")
    assert "3 entries with Apollo:" in out
    assert "2 paraphrased" in out
    assert "1 disputed" in out


@patch("apollo.overseer.diagnostic.OpenAI")
def test_generate_diagnostic_omits_negotiation_line_when_zero(mock_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("Looks good.")
    mock_cls.return_value = client

    out = generate_diagnostic(
        coverage=_coverage(),
        solver_result={"status": "solved"},
        reference_steps=[],
        problem_text="x",
        rubric={"overall": {"score": 80}},
    )
    assert out == "Looks good."
    assert "negotiated" not in out
