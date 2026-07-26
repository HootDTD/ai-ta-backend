"""Fixtures for the docs owns-coverage lint tests.

The pure builders (git mini-repo + frontmatter helpers) live in
``tests.support.docs_builders`` so they can be imported absolutely by the test
modules; this conftest only exposes them as pytest fixtures.
"""

from __future__ import annotations

import pytest

from tests.support.docs_builders import DocRepo, baseline_files


@pytest.fixture
def new_repo(tmp_path):
    """Factory: build a git repo from a files dict (staged, uncommitted)."""

    def _make(files: dict, *, name: str = "repo", commit: bool = False) -> DocRepo:
        repo = DocRepo(tmp_path / name)
        repo.write_many(files).add_all()
        if commit:
            repo.commit("initial")
        return repo

    return _make


@pytest.fixture
def baseline_repo(new_repo):
    """A ready-made valid repo (zero-finding tree)."""
    return new_repo(baseline_files())
