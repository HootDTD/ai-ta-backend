"""Diff-at-Done v1: per-turn chat is nodify + dumb reply only.

The full end-to-end handle_chat test (test_chat.py) is skipped legacy-V2 and
awaits a V3 KGGraph + Neo4j fixture rewrite (claude_v3_checklist.md item 1).
Until that lands, these module-level guards lock in the v1 contract change:
no output filter, no sufficiency / misconception / OLM-invite machinery, and
a slimmed response envelope.
"""
import inspect

from apollo.handlers import chat


def test_handle_chat_is_async():
    assert inspect.iscoroutinefunction(chat.handle_chat)


def test_chat_module_drops_signal_helpers():
    # Per-turn signal machinery is removed in v1.
    for gone in (
        "_build_sufficiency_verdict",
        "_signal_to_metadata",
        "_metadata_to_signal",
        "_load_previous_signals",
        "validate_or_raise",
        "check_sufficiency",
        "infer_misconception",
        "decide_invite",
    ):
        assert not hasattr(chat, gone), f"{gone} should be gone from chat module"


def test_chat_source_has_no_signal_envelopes_or_filter():
    src = inspect.getsource(chat)
    # The response envelope must not carry the removed signal keys
    # (quoted dict keys — explanatory comments naming them are fine).
    assert '"sufficiency":' not in src
    assert '"misconception":' not in src
    assert '"olm_invite":' not in src
    # The output filter call is gone.
    assert "validate_or_raise(" not in src
    # The dumb reply is fed the problem statement directly.
    assert "problem_text=problem.problem_text" in src
