"""The tally-state enum, across every copy — including the ones wave 1 missed.

Spec §4's testing contract names **four** copies (``unified.py`` twice,
``controller.py``, ``transcript_coverage.py``) and W1-A's
``apollo/smart_questions/tests/test_tally_state_enum_sync.py`` pins exactly
those. Drift between them fails SILENTLY: both readers coerce an unknown state
to ``"missing"``, so a divergence quietly downgrades live tally rows — P1.3's
prior context evaporates and the node looks untaught.

**What this gate adds (W3-C's extension).** P3.2 shipped two more references to
the same enum, neither of which the four-copy test covers, and both of which fail
silently in the same direction:

* ``smart_questions/challenge.py::_UNDERSTOOD`` — the done-gate's shape-(b)
  predicate (``state == _UNDERSTOOD and times_asked == 0``). A rename on the
  producer side would make the gate simply never fire, at level 2, with no error
  anywhere.
* ``smart_questions/selection.py::_MASTERED_STATUS`` — the askability filter. A
  rename there makes EVERY node askable, forever.

Both are single-member aliases rather than whole-enum copies, so they are pinned
as membership, plus a scan that fails when a SIXTH reference appears — the point
being that adding one becomes a deliberate act rather than an accident.

**Scan scope, and why it is not repo-wide.** ``"understood"`` is also a
teacher-facing bucket label in ``projections/performance_problems`` (mapped from
``coverage``'s ``"covered"``, a different vocabulary), and ``"missing"`` is the
``per_step`` coverage status in a dozen modules. Scanning those would pin noise.
The scan covers the modules that own the tally lifecycle — the questioning
package plus the two overseer modules that read a tally state — and treats
``conflicting`` / ``tentative`` / ``understood`` as the tally-specific tokens.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from apollo.overseer import transcript_coverage, wrongness
from apollo.smart_questions import challenge, controller, selection, unified

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Tally-specific tokens. ``"missing"`` is deliberately excluded — it is also the
#: `per_step` coverage vocabulary and would make the scan useless.
_TALLY_TOKENS = frozenset({"understood", "tentative", "conflicting"})

#: Modules that own the tally lifecycle. A new tally-state reference outside this
#: list is a different enum; inside it, it is a copy that must stay in sync.
_SCANNED = (
    "apollo/smart_questions/unified.py",
    "apollo/smart_questions/controller.py",
    "apollo/smart_questions/selection.py",
    "apollo/smart_questions/challenge.py",
    "apollo/smart_questions/prompts.py",
    "apollo/overseer/transcript_coverage.py",
    "apollo/overseer/wrongness.py",
)

#: The copies that exist TODAY, as `path -> the tokens it references`.
#: Wave 1 pinned the first three (the "four copies", counting `unified.py`'s
#: Literal and `_VALID_STATES` separately); the last two are W3-C's extension.
_EXPECTED_COPIES = {
    "apollo/smart_questions/unified.py": {"understood", "tentative", "conflicting"},
    "apollo/smart_questions/controller.py": {"understood", "tentative", "conflicting"},
    "apollo/overseer/transcript_coverage.py": {"understood", "tentative", "conflicting"},
    "apollo/smart_questions/selection.py": {"understood"},
    "apollo/smart_questions/challenge.py": {"understood"},
}


def _string_constants(path: Path) -> set[str]:
    """String LITERALS only — a docstring mentioning ``understood`` in prose (or
    in double backticks, the house style) is not a copy of the enum."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


def test_gate_enum_four_copies_of_the_tally_state_enum_agree():
    """W1-A's assertion, re-asserted at gate level so the §4 testing contract is
    satisfied from inside the named gate suite."""
    literal_states = set(get_args(unified.LearnerState))

    assert literal_states == unified._VALID_STATES
    assert literal_states == controller._VALID_STATES
    assert literal_states == set(transcript_coverage._VALID_TALLY_STATES)
    assert literal_states == {"understood", "tentative", "missing", "conflicting"}


def test_gate_enum_the_json_schema_offers_exactly_those_states():
    """A fifth surface: what the model is ALLOWED to send. A schema that offers a
    state no reader accepts produces silent coercion to ``missing``."""
    item = unified._schema()["schema"]["properties"]["tally_updates"]["items"]

    assert set(item["properties"]["status"]["enum"]) == unified._VALID_STATES


def test_gate_enum_the_single_member_aliases_are_members_of_the_enum():
    """W3-C's extension: the two P3.2 aliases the four-copy test does not reach."""
    assert challenge._UNDERSTOOD in unified._VALID_STATES
    assert selection._MASTERED_STATUS in unified._VALID_STATES
    assert challenge._UNDERSTOOD == selection._MASTERED_STATUS == "understood"


def test_gate_enum_the_gate_and_the_selector_agree_on_understood():
    """They must be the SAME member, not merely both valid: the done-gate's
    shape-(b) predicate fires on nodes ``build_selection_policy`` excluded for
    being mastered, so a divergence would make the gate target a node the policy
    never admits (or never fire at all)."""
    assert challenge._UNDERSTOOD == selection._MASTERED_STATUS


def test_gate_enum_wrongness_coercion_keys_on_a_real_member():
    """``wrongness._coerce_wrongness`` refuses a label on a ``missing`` node.
    That guard is a bare literal, so it is pinned as membership too — if the
    enum renamed ``missing``, the guard would stop firing and a producer bug
    would become a finding."""
    finding = wrongness.ledger_findings(
        [
            type(
                "_Row",
                (),
                {
                    "reference_node_id": "n1",
                    "state": "missing",
                    "times_asked": 0,
                    "last_asked_turn": None,
                    "evidence": [
                        {
                            "turn_id": 1,
                            "quote": "anything",
                            "wrongness": wrongness.WRONGNESS_MATERIAL,
                            "contradicts": "x",
                            "kind": "k",
                        }
                    ],
                },
            )()
        ]
    )

    assert "missing" in unified._VALID_STATES
    assert finding[0].wrongness == wrongness.WRONGNESS_NONE


@pytest.mark.parametrize("relative", _SCANNED)
def test_gate_enum_no_undeclared_extra_copy(relative):
    """A SIXTH reference must be a deliberate act. This fails when a tally-state
    literal appears in a lifecycle module that is not already declared above —
    at which point the author either adds it to ``_EXPECTED_COPIES`` (and to the
    membership assertions) or moves it behind the shared enum."""
    found = _string_constants(_REPO_ROOT / relative) & _TALLY_TOKENS

    assert found == _EXPECTED_COPIES.get(relative, set()), (
        f"{relative} references tally states {sorted(found)}; the pinned set is "
        f"{sorted(_EXPECTED_COPIES.get(relative, set()))}"
    )


def test_gate_enum_the_scanner_would_actually_catch_a_new_copy(tmp_path):
    """Control: a scanner that returned nothing would pass every case above."""
    module = tmp_path / "fake.py"
    module.write_text(
        '"""A docstring mentioning conflicting states in prose."""\n'
        '_MASTERED = "understood"\n'
        'IRRELEVANT = "covered"\n',
        encoding="utf-8",
    )

    assert _string_constants(module) & _TALLY_TOKENS == {"understood"}


def test_gate_enum_the_wrongness_enum_agrees_with_its_literal_and_its_schema():
    """The SECOND enum P3.2 introduced, duplicated for the same import-light
    reason (``unified.py`` and ``wrongness.py``) and pinned equal here."""
    assert set(get_args(unified.Wrongness)) == unified.WRONGNESS_VALUES
    assert unified.WRONGNESS_VALUES == wrongness.WRONGNESS_VALUES
    item = unified._schema(wrongness=True)["schema"]["properties"]["tally_updates"]["items"]
    assert set(item["properties"]["wrongness"]["enum"]) == unified.WRONGNESS_VALUES
