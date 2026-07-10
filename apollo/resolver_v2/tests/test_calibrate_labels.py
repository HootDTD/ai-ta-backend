"""T8 smoke test — §9 gold-label derivation in ``scripts/resolver_v2_calibrate.py``.

Card contract: label derivation from a 2-record fixture jsonl — positive /
negative sets per the §9 rules, including the control personas' per-node
negatives (misconception personas teach 4/5 beats correctly, so their
negatives are per-node, not whole-attempt zeros). Offline: no NLI, no model,
no DB.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The calibration script lives in scripts/ (a non-package dir) — same import
# shim scripts/tests/test_generate_resolver_v2_views.py uses.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import resolver_v2_calibrate as cal  # noqa: E402  (path-dependent import)

PATH_KEYS = frozenset({"eq.a", "eq.b", "proc.c", "cond.d"})

_STRONG_RECORD = {
    "persona": "strong",
    "status": "ok",
    "expected": {
        "credited": ["eq.a", "eq.b", "proc.c", "off_path.z"],
        "unresolved": ["cond.d"],
        "misconceptions": [],
    },
    "transcript": [
        {"role": "student", "content": "The equation A equals B."},
        {"role": "apollo", "content": "Why does that hold?"},
        {"role": "student", "content": "Because mass is conserved."},
    ],
}

_CONTROL_RECORD = {
    "persona": "misconception",
    "status": "ok",
    "expected": {
        "credited": ["eq.a", "eq.b", "proc.c"],
        "unresolved": [],
        "misconceptions": ["misc.density_ignored"],
    },
    "transcript": [{"role": "student", "content": "Pressure rises when pipes narrow."}],
}


def test_derive_labels_strong_persona_positives_and_unresolved_negatives():
    positives, negatives = cal.derive_labels(_STRONG_RECORD, PATH_KEYS)
    # credited ∩ path keys — the off-path key is dropped.
    assert positives == frozenset({"eq.a", "eq.b", "proc.c"})
    # non-control: negatives come from expected.unresolved ONLY.
    assert negatives == frozenset({"cond.d"})


def test_derive_labels_control_persona_gets_per_node_negatives():
    positives, negatives = cal.derive_labels(_CONTROL_RECORD, PATH_KEYS)
    assert positives == frozenset({"eq.a", "eq.b", "proc.c"})
    # control rule: every path key NOT credited is a negative (misc.* keys
    # are never path keys, so the misconception itself adds nothing here).
    assert negatives == frozenset({"cond.d"})
    # positives and negatives never overlap by construction.
    assert not (positives & negatives)


def test_load_calibration_records_two_record_fixture(tmp_path):
    # A record without a single student turn is excluded; error-status records
    # WITH student turns are kept (truncated-transcript note in the docstring).
    no_turns = {
        "persona": "vague_then_clarifies",
        "status": "error",
        "expected": {"credited": ["eq.a"], "unresolved": []},
        "transcript": [{"role": "apollo", "content": "Hello?"}],
    }
    fixture = tmp_path / "attempts.jsonl"
    fixture.write_text(
        "\n".join(json.dumps(r) for r in (_STRONG_RECORD, _CONTROL_RECORD, no_turns)) + "\n",
        encoding="utf-8",
    )
    records = cal.load_calibration_records(fixture)
    assert [r["persona"] for r in records] == ["strong", "misconception"]
    assert cal.student_turns_of(records[0]) == (
        "The equation A equals B.",
        "Because mass is conserved.",
    )
