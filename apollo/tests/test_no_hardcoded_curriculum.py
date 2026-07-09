"""Source-grep guard for the §8A curriculum cutover (WU-3D criterion #2).

Reads the actual source files and asserts the deleted hard-coded symbols and
filesystem-read patterns are ABSENT from the SELECTION path. If a deletion was
missed, these fail — fix the source, not the test. (The authoring-format module
``apollo/subjects/__init__.py`` legitimately keeps its file readers and is NOT
checked here.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_APOLLO = Path(__file__).resolve().parents[1]
_SESSION_INIT = _APOLLO / "hoot_bridge" / "session_init.py"
_PROBLEM_SELECTOR = _APOLLO / "overseer" / "problem_selector.py"
_CONCEPT_INFERENCE = _APOLLO / "overseer" / "concept_inference.py"
_CHAT = _APOLLO / "handlers" / "chat.py"
_DONE = _APOLLO / "handlers" / "done.py"
_NEXT = _APOLLO / "handlers" / "next.py"
_LIFECYCLE = _APOLLO / "handlers" / "lifecycle.py"
_HANDLERS = (_CHAT, _DONE, _NEXT, _LIFECYCLE)


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_available_clusters_constant_deleted():
    """T6.1 — _AVAILABLE_CLUSTERS is gone from session_init.py."""
    assert "_AVAILABLE_CLUSTERS" not in _src(_SESSION_INIT)


def test_cluster_to_concept_deleted():
    """T6.2 — the legacy cluster map / functions are gone from problem_selector
    and no handler imports them."""
    ps = _src(_PROBLEM_SELECTOR)
    assert "_CLUSTER_TO_CONCEPT" not in ps
    assert "def cluster_to_concept" not in ps
    assert "list_problems_for_cluster" not in ps

    ci = _src(_CONCEPT_INFERENCE)
    assert "infer_concept_cluster" not in ci
    assert "_AVAILABLE_CLUSTERS" not in ci

    for handler in _HANDLERS:
        text = _src(handler)
        assert "cluster_to_concept" not in text
        assert "infer_concept_cluster" not in text
        assert "list_problems_for_cluster" not in text


def test_no_filesystem_concept_read_in_selection_path():
    """T6.3 — no filesystem concept/problem reads remain on the selection path."""
    for path in (_PROBLEM_SELECTOR, _SESSION_INIT):
        text = _src(path)
        assert "from apollo.subjects import" not in text
        assert "load_concept(" not in text
        assert "load_problem(" not in text
        assert ".glob(" not in text

    # The handlers resolve the concept from the DB loader, not the filesystem.
    for handler in _HANDLERS:
        text = _src(handler)
        assert "load_concept(" not in text
        assert "load_problem(" not in text
        assert ".glob(" not in text
