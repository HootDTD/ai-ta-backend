"""P3.2 producer: the schema/prompt delta, and the level-0 byte-identity pins.

Level 0 of `APOLLO_WRONGNESS_LEVEL` must be byte-identical to the pre-P3.2
build **by construction, not by test luck**. The two sha256 pins below were
taken from `origin/staging @7c51fbe` BEFORE any P3.2 edit landed; the wrongness
schema fields and the WRONGNESS DUTY block are additive and gated, so they can
never perturb the level-0 call. If one of these pins reddens, the producer
changed the level-0 contract and the whole inertness argument (G-L1) is void.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from apollo.smart_questions import prompts, unified

pytestmark = pytest.mark.unit

# Taken from `origin/staging @7c51fbe`, pre-P3.2:
#   sha256(json.dumps(_schema(), sort_keys=True))
_SCHEMA_SHA256 = "8c31b43432017b3bfc9b6588cb75211ac5f9d9c189a5f9e8675d81c63b6923d6"
#   sha256(_SYSTEM_PROMPT)
_SYSTEM_PROMPT_SHA256 = "7784ed3ea22f642e1a33aeb429a1f1d6670855c81fb4f8b6e8c04121ab8f40f0"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Level-0 byte identity
# --------------------------------------------------------------------------- #
def test_schema_byte_identical_when_wrongness_off():
    assert _sha256(json.dumps(unified._schema(), sort_keys=True)) == _SCHEMA_SHA256
    assert unified._schema() == unified._schema(wrongness=False)


def test_system_prompt_byte_identical_when_wrongness_off():
    assert _sha256(prompts.build_system_prompt()) == _SYSTEM_PROMPT_SHA256
    assert prompts.build_system_prompt(wrongness=False) is prompts.SYSTEM_PROMPT


def test_base_messages_carry_the_level_0_prompt_unchanged():
    """The gate is at the call boundary: an un-flagged turn never builds a
    wrongness-bearing system turn."""
    messages = unified._base_messages({"public_problem": "x"})
    assert _sha256(messages[0]["content"]) == _SYSTEM_PROMPT_SHA256


# --------------------------------------------------------------------------- #
# The level->=1 delta
# --------------------------------------------------------------------------- #
def test_schema_adds_required_wrongness_and_contradiction_when_on():
    item = unified._schema(wrongness=True)["schema"]["properties"]["tally_updates"]["items"]
    # `additionalProperties: false` means declaring them is the ONLY way in, and
    # strict mode means every declared property must also be `required`.
    assert item["additionalProperties"] is False
    assert item["required"] == ["node_id", "status", "evidence", "wrongness", "contradiction"]
    assert item["properties"]["wrongness"]["enum"] == [
        "contradicts_material",
        "contradicts_self",
        "none",
    ]
    contradiction = item["properties"]["contradiction"]
    # Nullable, because "required iff wrongness != none" is NOT expressible in a
    # static `required` list — `_decode_updates` enforces the conditional.
    assert contradiction["type"] == ["object", "null"]
    assert contradiction["additionalProperties"] is False
    assert contradiction["required"] == ["reference_clause", "kind"]
    assert set(contradiction["properties"]) == {"reference_clause", "kind"}


def test_schema_enum_matches_the_value_object_enum():
    item = unified._schema(wrongness=True)["schema"]["properties"]["tally_updates"]["items"]
    assert set(item["properties"]["wrongness"]["enum"]) == unified.WRONGNESS_VALUES


def test_wrongness_prompt_block_defines_the_label_and_the_non_contradictions():
    prompt = prompts.build_system_prompt(wrongness=True)
    assert prompt.startswith(prompts.SYSTEM_PROMPT)
    assert "WRONGNESS DUTY:" in prompt
    for value in sorted(unified.WRONGNESS_VALUES):
        assert value in prompt, value
    lowered = prompt.lower()
    # Uncertainty/hedging/vagueness/silence are explicitly NOT contradictions —
    # the single loudest false-positive source if the prompt left it implicit.
    for word in ("uncertainty", "hedging", "vagueness", "silence"):
        assert word in lowered, word
    # `kind` is observability-only and gates nothing.
    assert "free-form" in lowered


def test_prompt_block_contains_no_real_student_text():
    """Mirrors `test_transcript_coverage_exemplars.py`: every exemplar is
    PARAPHRASED and invented.

    An exemplar copied out of a transcript would ride that pilot student's own
    words along in every questioning call this service ever makes. The blocked
    fragments are real prod student text — the P1 adjudicator blocklist plus
    attempt 167's self-correction sentence, which is P3.2's own named
    regression fixture (§2.2) and therefore the likeliest thing for a future
    editor to paste in as "a great example".
    """
    prompt = prompts.build_system_prompt(wrongness=True).lower()
    for verbatim in (
        "i was wrong about governance",
        "the gap between the informed and uninformed widens",
        "the four ethical issues are",
        "who is picasso",
        "not sure that",
    ):
        assert verbatim not in prompt, verbatim
    # The illustrations must announce themselves as paraphrase, so the rule
    # survives the next editor who does not read this test.
    assert "paraphrased illustrations, never a real student's words" in prompt
