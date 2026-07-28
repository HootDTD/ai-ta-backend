"""Feature 1 — the compact "Ask Hoot" aside refresher prompt + its plumbing.

Two concerns:

1. ``ai.prompts.apollo_aside.apollo_aside_prompt`` — the prompt contract: it keeps
   the source-boundedness / citation-discipline / JSON-output anchors from the
   tutor prompt but drops every standalone-chat section (``## Answer``,
   Key-Takeaway, self-check) machinery.
2. ``ai.main_ai`` ``system_prompt_override`` — the keyword-only override threaded
   through ``_prepare_solve_prompt`` / ``solve_with_bundle``. ``None`` must produce
   a byte-identical system prompt to today (``tutor_prompt()``); a supplied value
   must be exactly what reaches the OpenAI chat call, with the user payload
   otherwise unchanged.
"""

from __future__ import annotations

import json
import types

import ai.main_ai as mai
from ai.prompts import apollo_aside_prompt, tutor_prompt
from config.contracts import ParsedTask, ResearchBundle, ResearchMetadata

# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------

# The shared user payload (ai.main_ai._prepare_solve_prompt) is byte-identical
# whether or not an override is supplied — see
# test_override_is_exactly_what_reaches_the_chat_call — and it hardcodes the
# tutor three-section instruction on every solve. So the aside prompt cannot rely
# on a divergent payload; it must NAME those sections and explicitly countermand
# them. This asserts the override is present and forceful, not that the tokens are
# absent (the previous, weaker contract, which the live model overrode).
_TUTOR_SECTIONS = ("## Answer", "## Key Takeaway", "## Check Your Understanding")


def test_aside_prompt_overrides_the_payload_three_section_structure():
    prompt = apollo_aside_prompt()

    assert "OUTPUT FORMAT OVERRIDE" in prompt
    assert "IGNORE it completely" in prompt
    # The three tutor sections appear ONLY so the prompt can forbid them by name.
    for section in _TUTOR_SECTIONS:
        assert section in prompt, f"aside prompt must name {section!r} to forbid it"
    # It must never carry the payload's *positive* "use all three sections" wording.
    assert "use all three sections" not in prompt
    assert "flowing plain prose" in prompt


def test_aside_prompt_keeps_source_boundedness_anchors():
    prompt = apollo_aside_prompt()

    assert "CORE OPERATING RULE" in prompt
    assert "Source-boundedness is mandatory" in prompt
    # No-outside-facts / no-numeric-fabrication survive from the tutor contract.
    assert "Base your response ONLY on the provided source excerpts." in prompt
    assert "Do NOT fabricate specific numbers" in prompt


def test_aside_prompt_keeps_citation_discipline_anchors():
    prompt = apollo_aside_prompt()

    assert "CITATION DISCIPLINE" in prompt
    assert "Use the exact citation marker format provided in the excerpts." in prompt
    assert "Prefer claim-level citations" in prompt


def test_aside_prompt_keeps_json_output_and_relevance_contract():
    prompt = apollo_aside_prompt()

    # The solver's JSON parsing is unchanged: not_relevant flag + single-string steps.
    assert "not_relevant" in prompt
    assert "The 'steps' field MUST be a single Markdown-formatted string, NOT an array." in prompt
    # LaTeX rules survive.
    assert "Display math: $$expression$$ on its own line." in prompt


def test_aside_prompt_keeps_scope_and_multipart_handling():
    prompt = apollo_aside_prompt()

    assert "SCOPE CONTROL" in prompt
    assert "MULTIPART / MIXED-RELEVANCE HANDLING" in prompt
    # Out-of-scope sentence contract preserved.
    assert "falls outside the course materials and cannot be addressed here." in prompt


def test_aside_prompt_has_the_refresher_voice_and_handoff():
    prompt = apollo_aside_prompt()

    # Mid-teaching-session framing + hand-back-to-teaching close.
    assert "Apollo" in prompt
    assert "own words" in prompt
    assert "compact" in prompt


def test_aside_prompt_enforces_single_statement_rule():
    """Defect 1 (repetition): every fact must appear exactly once, and
    overlapping excerpts get synthesized into one cited sentence."""
    prompt = apollo_aside_prompt()

    assert "SAY EACH FACT EXACTLY ONCE" in prompt
    assert "State each fact exactly once" in prompt
    # Overlapping-excerpt synthesis rather than a second restatement.
    assert "Synthesize the overlap into ONE sentence" in prompt
    # A worked good-vs-bad example is the strongest lever against this failure.
    assert "GOOD vs BAD" in prompt


def test_aside_prompt_bans_narrator_meta_references():
    """Defect 2 (auditor register): no "This course material states"-style
    narration of the source; say the thing and attach the citation."""
    prompt = apollo_aside_prompt()

    assert "NO NARRATOR VOICE" in prompt
    assert "Never write narrator phrases like" in prompt
    assert "This course material states" in prompt
    assert "According to" in prompt


def test_aside_prompt_forbids_citation_on_the_handback_sentence():
    """Defect 3: the closing hand-back sentence must carry no citation marker."""
    prompt = apollo_aside_prompt()

    assert "MUST NOT contain a citation marker" in prompt
    assert "The closing hand-back sentence makes no factual claim" in prompt


def test_aside_prompt_drops_copy_the_wording_pressure():
    """Root cause of the defensive re-quoting: the tutor prompt's
    prefer-copied-wording instruction must be gone from the aside voice."""
    prompt = apollo_aside_prompt()

    assert "Prefer concrete wording copied or closely paraphrased" not in prompt
    assert "do NOT need to copy or mirror the excerpt's wording" in prompt


def test_aside_prompt_differs_from_the_standalone_tutor_prompt():
    assert apollo_aside_prompt() != tutor_prompt()


# ---------------------------------------------------------------------------
# main_ai override plumbing
# ---------------------------------------------------------------------------


def _fake_client(captured: dict, content: str):
    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            msg = types.SimpleNamespace(content=content)
            choice = types.SimpleNamespace(message=msg)
            return types.SimpleNamespace(choices=[choice])

    chat = types.SimpleNamespace(completions=_Completions())
    return types.SimpleNamespace(chat=chat)


def _empty_bundle() -> ResearchBundle:
    # Empty snippets skip the scoring pool; the prompt still builds fully.
    return ResearchBundle(metadata=ResearchMetadata(question="What is lift?"), snippets=[])


def _solve_capturing_messages(monkeypatch, **override_kwargs) -> list[dict]:
    captured: dict = {}
    monkeypatch.setattr(mai, "_client", lambda: _fake_client(captured, json.dumps({"steps": "ok"})))
    mai.solve_with_bundle(
        ParsedTask(problem_type="conceptual"),
        _empty_bundle(),
        subject="Physics",
        **override_kwargs,
    )
    return captured["messages"]


def test_override_none_is_byte_identical_to_no_override(monkeypatch):
    """Same inputs, built both ways, must be byte-identical — a None override
    cannot move a single character of the system OR user prompt."""
    baseline = _solve_capturing_messages(monkeypatch)
    with_none = _solve_capturing_messages(monkeypatch, system_prompt_override=None)

    assert with_none == baseline
    assert baseline[0]["content"] == tutor_prompt()


def test_override_is_exactly_what_reaches_the_chat_call(monkeypatch):
    """A supplied override becomes the system message verbatim, AND the user
    payload's `steps` shaping defers to it instead of hardcoding the tutor's
    three-section structure (which a system-prompt-only override could not
    reliably beat at the model level)."""
    sentinel = "SENTINEL-ASIDE-SYSTEM-PROMPT"
    baseline = _solve_capturing_messages(monkeypatch)
    overridden = _solve_capturing_messages(monkeypatch, system_prompt_override=sentinel)

    assert overridden[0]["content"] == sentinel

    baseline_user = baseline[1]["content"]
    overridden_user = overridden[1]["content"]
    # Today's Hoot-chat payload (no override) still names the three tutor sections.
    assert "## Answer, ## Key Takeaway, ## Check Your Understanding" in baseline_user
    assert "use all three sections" in baseline_user
    # The override payload drops that structure demand and defers to the system prompt.
    assert "## Answer, ## Key Takeaway, ## Check Your Understanding" not in overridden_user
    assert "use all three sections" not in overridden_user
    assert "defer" in overridden_user or "Follow the RESPONSE SHAPE" in overridden_user
    # The steps JSON contract (single Markdown string, LaTeX) survives on both paths.
    assert "a SINGLE Markdown-formatted string (NOT an array)" in overridden_user
    assert "$$...$$ display" in overridden_user


def test_apollo_aside_prompt_flows_through_solve(monkeypatch):
    """End-to-end: the real aside prompt is what the solver would send."""
    messages = _solve_capturing_messages(monkeypatch, system_prompt_override=apollo_aside_prompt())

    assert messages[0]["content"] == apollo_aside_prompt()


def test_prepare_solve_prompt_default_keeps_tutor_prompt():
    """The lower-level builder still defaults to the tutor system prompt."""
    system, _user, _model = mai._prepare_solve_prompt(
        ParsedTask(problem_type="conceptual"), _empty_bundle(), subject="Physics"
    )

    assert system == tutor_prompt()
