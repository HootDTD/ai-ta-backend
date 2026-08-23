"""P0.4 consent outranks anti-gaming: a STUDENT-initiated Done is never blocked.

This is the guard the done-gate must not be able to break, so it is asserted
STRUCTURALLY — over the real call graph — rather than by exercising one happy
path. A student reaches grading two ways, and neither one passes through the
questioning engine:

* ``POST /done`` -> ``api`` -> ``handlers.done.handle_done``;
* an affirmed ``done`` intent -> ``chat._handle_pending_done`` -> ``handle_done``.

``plan_next_question`` — and therefore ``evaluate_and_ask``, and therefore the
gate — is only ever reached from the ordinary teaching turn. The only ``done``
the gate can override is one APOLLO decided for itself.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from apollo.handlers import chat, done
from apollo.smart_questions import controller, unified


def _module_tree(module) -> ast.Module:
    return ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))


def _called_names(node: ast.AST) -> set[str]:
    """Every name invoked anywhere inside `node`, attribute calls included."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        target = child.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _function(module, name: str) -> ast.AST:
    for node in ast.walk(_module_tree(module)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{module.__name__}.{name} not found")


_QUESTIONING_ENTRY_POINTS = {"plan_next_question", "evaluate_and_ask", "resolve"}


def test_the_call_scanner_actually_detects_a_call():
    """Control for the three structural assertions below: a scanner that finds
    nothing would pass every one of them for the wrong reason."""
    source = ast.parse(
        "async def f():\n"
        "    await plan_next_question(db)\n"
        "    await mod.evaluate_and_ask()\n"
        "    return 1\n"
    )
    assert _called_names(source) == {"plan_next_question", "evaluate_and_ask"}
    assert not _called_names(source).isdisjoint(_QUESTIONING_ENTRY_POINTS)


def test_student_done_path_never_calls_evaluate_and_ask():
    """`handle_done` is the whole student-initiated grade path. If it ever grows
    a questioning call, a student who clicked Done could be answered with a
    question instead of a grade."""
    called = _called_names(_function(done, "handle_done"))
    assert called, "scanner found no calls at all in handle_done"
    assert called.isdisjoint(_QUESTIONING_ENTRY_POINTS)


def test_the_affirmed_done_intent_path_never_calls_the_questioning_engine():
    called = _called_names(_function(chat, "_handle_pending_done"))
    assert called.isdisjoint(_QUESTIONING_ENTRY_POINTS)
    # It dispatches straight to grading.
    assert "handle_done" in called


def test_done_module_never_imports_the_questioning_engine():
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(_module_tree(done))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        name
        for node in ast.walk(_module_tree(done))
        if isinstance(node, ast.ImportFrom)
        for name in [alias.name for alias in node.names] + [node.module or ""]
    }
    assert not any("smart_questions" in name for name in imported)
    assert "plan_next_question" not in imported


def test_gate_never_touches_handle_done():
    """The gate lives strictly inside the questioning call: neither `unified`
    nor `controller` can reach the grading path."""
    for module in (unified, controller):
        called = _called_names(_module_tree(module))
        assert "handle_done" not in called
        assert "_grade_claimed_attempt" not in called


def test_only_the_ordinary_teaching_turn_reaches_plan_next_question():
    """One call site in `chat`, and it is the turn handler — not either Done."""
    tree = _module_tree(chat)
    holders = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and "plan_next_question" in _called_names(node)
    }
    assert "_handle_pending_done" not in holders
    assert holders, "plan_next_question should still be reachable from chat"
