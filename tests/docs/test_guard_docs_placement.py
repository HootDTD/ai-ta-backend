"""Unit coverage for .claude/hooks/guard_docs_placement.py.

The hook is a PreToolUse guard that reads a JSON event on stdin and warns (never
blocks — always exits 0) when a NEW .md is Written under docs/ outside the
allowed durable/quarantine locations. ``.claude/`` is not a package, so we load
the module by path via importlib (conventional for hook tests).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

import pytest

pytestmark = pytest.mark.unit

_HOOK_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", ".claude", "hooks", "guard_docs_placement.py"
    )
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("guard_docs_placement", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_hook()


def _run(monkeypatch, stdin_text: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    return guard.main()


def _event(**kw) -> str:
    return json.dumps(kw)


# --------------------------------------------------------------------------- #
# Early-return guards
# --------------------------------------------------------------------------- #
def test_invalid_json_returns_zero(monkeypatch):
    assert _run(monkeypatch, "not-json{{{") == 0


def test_non_write_tool_returns_zero(monkeypatch, capsys):
    rc = _run(monkeypatch, _event(tool_name="Edit", tool_input={"file_path": "docs/x.md"}))
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_missing_tool_input_returns_zero(monkeypatch):
    # tool_input is None -> the `or {}` guard keeps file_path == "".
    assert _run(monkeypatch, _event(tool_name="Write", tool_input=None)) == 0


def test_non_docs_path_returns_zero(monkeypatch, capsys):
    rc = _run(
        monkeypatch,
        _event(tool_name="Write", tool_input={"file_path": "src/module.py"}),
    )
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_planning_md_is_exempt_via_docs_substring(monkeypatch, capsys):
    # .planning/ docs contain no "/docs/" segment -> exempt at the docs gate.
    rc = _run(
        monkeypatch,
        _event(tool_name="Write", tool_input={"file_path": ".planning/handoffs/x.md"}),
    )
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_docs_non_md_returns_zero(monkeypatch, capsys):
    rc = _run(
        monkeypatch,
        _event(tool_name="Write", tool_input={"file_path": "repo/docs/index.json"}),
    )
    assert rc == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# Existing-file exemption
# --------------------------------------------------------------------------- #
def test_existing_file_is_exempt(monkeypatch, capsys, tmp_path):
    # A path that would otherwise be disallowed (loose docs/*.md) but already
    # exists on disk is exempt (editing, not creating).
    p = tmp_path / "docs" / "loose.md"
    p.parent.mkdir(parents=True)
    p.write_text("# existing\n", encoding="utf-8")
    rc = _run(
        monkeypatch,
        _event(tool_name="Write", tool_input={"file_path": str(p)}),
    )
    assert rc == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# Allowed durable/quarantine locations (new files)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "repo/docs/architecture/apollo/grader.md",
        "repo/docs/shared-architecture/security.md",
        "repo/docs/_archive/handoffs/note.md",
        "repo/docs/superpowers/plans/plan.md",
    ],
)
def test_allowed_new_doc_locations_are_silent(monkeypatch, capsys, path):
    rc = _run(monkeypatch, _event(tool_name="Write", tool_input={"file_path": path}))
    assert rc == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- #
# Disallowed new loose doc -> warns (still exits 0)
# --------------------------------------------------------------------------- #
def test_disallowed_new_loose_doc_warns(monkeypatch, capsys):
    rc = _run(
        monkeypatch,
        _event(tool_name="Write", tool_input={"file_path": "repo/docs/loose-note.md"}),
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING docs-placement" in err
    assert "repo/docs/loose-note.md" in err


def test_backslash_path_is_normalized(monkeypatch, capsys):
    # Windows-style separators still resolve to a disallowed loose doc warning.
    rc = _run(
        monkeypatch,
        _event(tool_name="Write", tool_input={"file_path": r"repo\docs\loose.md"}),
    )
    assert rc == 0
    assert "WARNING docs-placement" in capsys.readouterr().err
