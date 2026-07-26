"""Builders for the docs owns-coverage lint tests (no Docker / no network).

Pure helpers that construct throwaway git mini-repos and frontmatter doc trees
in a temp dir. Lives under ``tests/support`` so both ``tests/docs/conftest.py``
and the test modules can import it absolutely (mirrors ``tests.support.vcr``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git subcommand inside ``repo`` (UTF-8, captured)."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def _yaml_list(key: str, values) -> list[str]:
    if not values:
        return [f"{key}: []"]
    out = [f"{key}:"]
    out.extend(f"  - {v}" for v in values)
    return out


def leaf(
    doc_id: str,
    *,
    owns=(),
    related=(),
    description: str = "leaf doc",
    last_verified: str = "2026-07-25",
    stub: bool = False,
    interface: bool = True,
    body: str | None = None,
    omit=(),
) -> str:
    """A non-router leaf doc. Includes a ``## Interface`` section by default."""
    fm: list[str] = ["---"]
    if "doc" not in omit:
        fm.append(f"doc: {doc_id}")
    if "description" not in omit:
        fm.append(f"description: {description}")
    if "owns" not in omit:
        fm.extend(_yaml_list("owns", owns))
    if "related" not in omit:
        fm.extend(_yaml_list("related", related))
    if "last_verified" not in omit:
        fm.append(f"last_verified: {last_verified}")
    if "stub" not in omit:
        fm.append(f"stub: {'true' if stub else 'false'}")
    fm.append("---")
    if body is None:
        body = "# " + doc_id + "\n\n"
        if interface:
            body += "## Interface\n\nDetails.\n"
        else:
            body += "Prose only.\n"
    return "\n".join(fm) + "\n" + body


def router(
    doc_id: str,
    *,
    related=(),
    description: str = "router",
    last_verified: str = "2026-07-25",
    body: str = "",
    omit=(),
) -> str:
    """An ``_index.md`` / ``_overview.md`` router doc (owns is always empty)."""
    fm: list[str] = ["---"]
    if "doc" not in omit:
        fm.append(f"doc: {doc_id}")
    if "description" not in omit:
        fm.append(f"description: {description}")
    if "owns" not in omit:
        fm.append("owns: []")
    if "related" not in omit:
        fm.extend(_yaml_list("related", related))
    if "last_verified" not in omit:
        fm.append(f"last_verified: {last_verified}")
    if "stub" not in omit:
        fm.append("stub: false")
    fm.append("---")
    return "\n".join(fm) + "\n" + body


class DocRepo:
    """A minimal git-backed repo builder rooted at ``root``."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Docs Test")
        git(self.root, "config", "commit.gpgsign", "false")
        git(self.root, "config", "core.autocrlf", "false")

    def write(self, relpath: str, content: str) -> Path:
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="" -> exact bytes, LF preserved on every platform.
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return p

    def write_many(self, files: dict) -> DocRepo:
        for rel, content in files.items():
            self.write(rel, content)
        return self

    def add_all(self) -> DocRepo:
        git(self.root, "add", "-A")
        return self

    def commit(self, msg: str = "snapshot") -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", msg, "--no-gpg-sign")
        return git(self.root, "rev-parse", "HEAD").stdout.strip()


def baseline_files() -> dict:
    """A fully-valid tree that the lint passes with zero findings."""
    return {
        "pytest.ini": "[pytest]\n",
        "apollo/grader.py": "grade = 1\n",
        "rag/pipeline.py": "run = 2\n",
        "docs/architecture/_overview.md": router("overview", body="# Overview\n\nRoot router.\n"),
        "docs/architecture/apollo/_index.md": router(
            "apollo-index",
            body="# Apollo\n\n- [grader](grader.md)\n",
        ),
        "docs/architecture/apollo/grader.md": leaf(
            "apollo-grader", owns=["apollo/grader.py"], related=["rag-pipeline"]
        ),
        "docs/architecture/rag/_index.md": router(
            "rag-index",
            body="# RAG\n\n- [pipeline](pipeline.md)\n",
        ),
        "docs/architecture/rag/pipeline.md": leaf("rag-pipeline", owns=["rag/pipeline.py"]),
    }
