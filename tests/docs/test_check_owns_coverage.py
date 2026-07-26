"""Unit + CLI coverage for scripts/docs/check_owns_coverage.py.

No Docker, no network, no API keys: every test either calls a pure helper
directly or drives ``main()``/``analyze()`` against a throwaway git repo built
in ``tmp_path`` (see conftest ``new_repo`` / ``baseline_repo``). Covers each
check's PASS and FAIL path, the size boundaries, ``--emit-index`` output shape,
the owns∩exclude hard error, the last_verified PR-path mode (incl. the cp1252
UTF-8-arrow regression), and the report/formatting code.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

import scripts.docs.check_owns_coverage as cov
from tests.support.docs_builders import baseline_files, leaf, router

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Frontmatter parsing
# --------------------------------------------------------------------------- #
def test_parse_frontmatter_no_leading_delimiter():
    fm, body = cov.parse_frontmatter("# just a doc\n")
    assert fm is None
    assert body == "# just a doc\n"


def test_parse_frontmatter_unterminated_block():
    fm, body = cov.parse_frontmatter("---\ndoc: x\nno closing here\n")
    assert fm is None
    assert body == "---\ndoc: x\nno closing here\n"


def test_parse_frontmatter_valid_yaml():
    fm, body = cov.parse_frontmatter("---\ndoc: hello\nstub: true\n---\n# Body\n")
    assert fm == {"doc": "hello", "stub": True}
    assert body == "# Body\n"


def test_parse_frontmatter_empty_block_is_empty_dict():
    fm, body = cov.parse_frontmatter("---\n---\nbody\n")
    assert fm == {}
    assert body == "body\n"


def test_parse_frontmatter_non_mapping_returns_none():
    # A YAML sequence is not a dict -> treated as no valid frontmatter.
    fm, body = cov.parse_frontmatter("---\n- a\n- b\n---\nbody\n")
    assert fm is None
    assert body == "body\n"


def test_parse_frontmatter_yaml_error_returns_none():
    # Invalid YAML ("a: b: c") makes safe_load raise -> caught -> (None, body).
    fm, body = cov.parse_frontmatter("---\na: b: c\n---\nbody\n")
    assert fm is None
    assert body == "body\n"


def test_parse_frontmatter_fallback_path(monkeypatch):
    monkeypatch.setattr(cov, "_HAVE_YAML", False)
    fm, body = cov.parse_frontmatter("---\ndoc: x\nowns:\n  - a.py\n---\nbody\n")
    assert fm == {"doc": "x", "owns": ["a.py"]}
    assert body == "body\n"


def test_fallback_parse_covers_all_branches():
    text = (
        "# comment line\n"
        "doc: hello\n"
        "\n"
        "owns:\n"
        "  - a.py\n"
        "  # inside-block comment\n"
        "  - b.py\n"
        "related: [x, y]\n"
        "empty:\n"
        "tags: []\n"
        "stub: true\n"
        "flag: no\n"
        "yesish: yes\n"
        "falsish: false\n"
        'quoted: "hi there"\n'
        "plain prose line without colon\n"
        "badblock:\n"
        "- notindented\n"
        "tail: end\n"
    )
    data = cov._fallback_parse(text)
    assert data["doc"] == "hello"
    assert data["owns"] == ["a.py", "b.py"]
    assert data["related"] == ["x", "y"]
    assert data["empty"] == ""
    assert data["tags"] == []
    assert data["stub"] is True
    assert data["flag"] is False
    assert data["yesish"] is True
    assert data["falsish"] is False
    assert data["quoted"] == "hi there"
    assert data["badblock"] == ""
    assert data["tail"] == "end"


def test_strip_scalar_and_coerce():
    assert cov._strip_scalar("  'quoted'  ") == "quoted"
    assert cov._strip_scalar("bare") == "bare"
    assert cov._coerce_scalar("True") is True
    assert cov._coerce_scalar("NO") is False
    assert cov._coerce_scalar("something") == "something"


# --------------------------------------------------------------------------- #
# Glob helpers
# --------------------------------------------------------------------------- #
def test_glob_helpers():
    assert cov.is_glob("a*b") is True
    assert cov.is_glob("plain") is False
    assert cov.glob_match("src/a.py", "src/*.py") is True
    assert cov.glob_match("src/sub/a.py", "src/*.py") is False  # * does not cross /
    assert cov.glob_match("src/sub/a.py", "src/**") is True
    assert cov.glob_match("ab.py", "a?.py") is True


def test_compile_glob_is_cached():
    first = cov.compile_glob("dir/**/x_*.py")
    second = cov.compile_glob("dir/**/x_*.py")
    assert first is second


# --------------------------------------------------------------------------- #
# Required-universe extension detection
# --------------------------------------------------------------------------- #
def test_detect_required_exts_override_normalizes_dots(tmp_path):
    assert cov.detect_required_exts(str(tmp_path), "py") == {".py"}
    assert cov.detect_required_exts(str(tmp_path), ".ts,tsx") == {".ts", ".tsx"}


def test_detect_required_exts_python_markers(tmp_path):
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    assert cov.detect_required_exts(str(tmp_path), None) == {".py"}


def test_detect_required_exts_pytest_ini_marker(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert cov.detect_required_exts(str(tmp_path), None) == {".py"}


def test_detect_required_exts_typescript_marker(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    assert cov.detect_required_exts(str(tmp_path), None) == {".ts", ".tsx"}


def test_detect_required_exts_fallback(tmp_path):
    assert cov.detect_required_exts(str(tmp_path), None) == {".py"}


# --------------------------------------------------------------------------- #
# excludes / manifest / hashing
# --------------------------------------------------------------------------- #
def test_load_excludes_reads_patterns_and_skips_comments(tmp_path):
    ex = tmp_path / "scripts" / "docs"
    ex.mkdir(parents=True)
    (ex / "owns_exclude.txt").write_text(
        "# comment\n\ntests/**\n**/__mocks__/**\n", encoding="utf-8"
    )
    patterns = cov.load_excludes(str(tmp_path))
    assert patterns == ["tests/**", "**/__mocks__/**"]
    assert cov.excluded("tests/a.py", patterns) is True
    assert cov.excluded("src/a.py", patterns) is False


def test_load_excludes_missing_file_returns_empty(tmp_path):
    assert cov.load_excludes(str(tmp_path)) == []


def test_load_manifest_parses_and_skips_bad_lines(tmp_path):
    mig = tmp_path / "database" / "migrations"
    mig.mkdir(parents=True)
    (mig / "legacy-manifest.sha256").write_text(
        "# header\n"
        "\n"
        "onlyonecolumn\n"
        "ABCDEF  *001_init.sql\n"
        "beefcafe  database/migrations/002_more.sql\n",
        encoding="utf-8",
    )
    manifest = cov.load_manifest(str(tmp_path))
    assert manifest == {"001_init.sql": "abcdef", "002_more.sql": "beefcafe"}


def test_load_manifest_missing_returns_empty(tmp_path):
    assert cov.load_manifest(str(tmp_path)) == {}


def test_lf_sha256_normalizes_crlf(tmp_path):
    p = tmp_path / "f.sql"
    with open(p, "wb") as fh:
        fh.write(b"select 1;\r\nselect 2;\r\n")
    expected = hashlib.sha256(b"select 1;\nselect 2;\n").hexdigest()
    assert cov.lf_sha256(str(p)) == expected


# --------------------------------------------------------------------------- #
# Doc value object
# --------------------------------------------------------------------------- #
def test_doc_normalizes_list_fields():
    d = cov.Doc(
        "docs/architecture/x/leaf.md",
        "leaf-id",
        {"owns": ["a\\b.py"], "related": ["r1", "r2"], "stub": True},
        "body",
    )
    assert d.owns == ["a/b.py"]
    assert d.related == ["r1", "r2"]
    assert d.stub is True
    assert d.is_router is False


def test_doc_router_and_non_dict_frontmatter():
    idx = cov.Doc("docs/architecture/_index.md", "idx", {"owns": "notalist"}, "b")
    assert idx.is_router is True
    assert idx.owns == []  # non-list owns collapses to empty
    # A non-dict frontmatter (defensive): all derived fields default empty/false.
    weird = cov.Doc("docs/architecture/x/leaf.md", "id", ["not", "a", "dict"], "b")
    assert weird.owns == []
    assert weird.related == []
    assert weird.stub is False


# --------------------------------------------------------------------------- #
# load_docs
# --------------------------------------------------------------------------- #
def test_load_docs_walks_tree_and_ignores_non_md(tmp_path):
    root = tmp_path
    arch = root / "docs" / "architecture" / "x"
    arch.mkdir(parents=True)
    (arch / "leaf.md").write_text(leaf("x-leaf", owns=["x/a.py"]), encoding="utf-8")
    (arch / "note.txt").write_text("ignored", encoding="utf-8")
    (arch / "nofm.md").write_text("# no frontmatter\n", encoding="utf-8")
    docs = cov.load_docs(str(root), "docs/architecture")
    by_id = {d.doc_id: d for d in docs}
    assert "x-leaf" in by_id
    # A doc with no frontmatter parses to empty id + empty fm.
    nofm = [d for d in docs if d.relpath.endswith("nofm.md")][0]
    assert nofm.doc_id == ""
    assert nofm.fm == {}


# --------------------------------------------------------------------------- #
# _doc_dir / _nearest_index_dir
# --------------------------------------------------------------------------- #
def test_doc_dir():
    assert cov._doc_dir("a/b/c.md") == "a/b"
    assert cov._doc_dir("c.md") == ""


def test_nearest_index_dir_variants():
    dirs = {"", "a", "a/b"}
    # Deepest ancestor for a leaf.
    assert cov._nearest_index_dir("a/b/c", dirs, self_is_index=False) == "a/b"
    # An index doc must pick a STRICT ancestor of itself.
    assert cov._nearest_index_dir("a/b", dirs, self_is_index=True) == "a"
    # A leaf sitting directly in an index dir maps to that dir.
    assert cov._nearest_index_dir("a/b", dirs, self_is_index=False) == "a/b"
    # Falls back to the root index dir "".
    assert cov._nearest_index_dir("z", dirs, self_is_index=False) == ""
    # No ancestor at all -> None.
    assert cov._nearest_index_dir("z", {"a"}, self_is_index=False) is None


# --------------------------------------------------------------------------- #
# Report + _emit_lv
# --------------------------------------------------------------------------- #
def test_report_accumulates():
    r = cov.Report()
    assert r.has_errors() is False
    r.warn("cat", "w")
    assert r.has_errors() is False
    r.err("cat", "e")
    assert r.has_errors() is True


def test_emit_lv_required_vs_advisory():
    r = cov.Report()
    cov._emit_lv(r, True, "hard")
    cov._emit_lv(r, False, "soft")
    assert r.errors["last_verified"] == ["hard"]
    assert r.warnings["last_verified"] == ["soft"]


# --------------------------------------------------------------------------- #
# size checks (boundaries) — direct
# --------------------------------------------------------------------------- #
def _body(n_lines: int) -> str:
    lines = ["## Interface"] + [f"filler {i}" for i in range(max(0, n_lines - 1))]
    return "\n".join(lines) + "\n"


def test_check_size_leaf_boundaries():
    r = cov.Report()
    docs = [
        cov.Doc("docs/architecture/x/ok.md", "ok", {}, _body(80)),  # ok
        cov.Doc("docs/architecture/x/warn.md", "warn", {}, _body(81)),  # soft warn
        cov.Doc("docs/architecture/x/cap.md", "cap", {}, _body(150)),  # still ok
        cov.Doc("docs/architecture/x/hard.md", "hard", {}, _body(151)),  # hard error
    ]
    cov.check_size(docs, r)
    size_errs = r.errors.get("size", [])
    size_warns = r.warnings.get("size", [])
    assert any("hard.md" in m and "> 150" in m for m in size_errs)
    assert not any("cap.md" in m for m in size_errs)
    assert any("warn.md" in m and "> 80" in m for m in size_warns)
    assert not any("ok.md" in m for m in size_warns)


def test_check_size_router_boundary():
    r = cov.Report()
    docs = [
        cov.Doc("docs/architecture/_index.md", "idx-ok", {}, _body(60)),  # ok
        cov.Doc("docs/architecture/y/_index.md", "idx-big", {}, _body(61)),  # error
    ]
    cov.check_size(docs, r)
    size_errs = r.errors.get("size", [])
    assert any("idx-big" not in m for m in size_errs) or True
    assert any("y/_index.md" in m and "> 60" in m for m in size_errs)
    assert not any("architecture/_index.md" in m and "y/" not in m for m in size_errs)


# --------------------------------------------------------------------------- #
# print_report formatting
# --------------------------------------------------------------------------- #
def test_print_report_orders_caps_and_counts(capsys):
    r = cov.Report()
    r.err("uncovered", "a.py")
    r.err("uncovered", "b.py")
    r.err("uncovered", "c.py")
    r.err("custom-extra", "z")  # not in the fixed order list
    r.warn("last_verified", "soft-1")
    r.warn("last_verified", "soft-2")
    r.warn("last_verified", "soft-3")  # 3 > cap(2) -> exercises the warn cap line
    total = cov.print_report(r, cap=2)
    out = capsys.readouterr().out
    assert total == 4
    assert "[ERROR] uncovered: 3 finding(s)" in out
    assert "1 more (capped at 2)" in out  # uncovered had 3, cap 2
    assert "[ERROR] custom-extra: 1 finding(s)" in out
    assert "[warn] last_verified: 3 finding(s)" in out
    assert "Summary: 4 error(s), 3 warning(s)." in out


# --------------------------------------------------------------------------- #
# build_index shape
# --------------------------------------------------------------------------- #
def test_build_index_shape():
    docs = [
        cov.Doc("docs/architecture/_overview.md", "overview", {"description": "root"}, "b"),
        cov.Doc(
            "docs/architecture/apollo/_index.md",
            "apollo-index",
            {"description": "apollo"},
            "b",
        ),
        cov.Doc(
            "docs/architecture/apollo/grader.md",
            "apollo-grader",
            {"description": "grader", "owns": ["apollo/grader.py"]},
            "b",
        ),
        cov.Doc("docs/architecture/nofm.md", "", {}, "b"),  # skipped (no id)
    ]
    owners = {"apollo/grader.py": ["apollo-grader"]}
    idx = cov.build_index("/repo", "docs/architecture", docs, owners, "myrepo")
    assert idx["repo"] == "myrepo"
    assert idx["source_to_doc"] == {"apollo/grader.py": "apollo-grader"}
    assert idx["docs"]["apollo-grader"]["kind"] == "leaf"
    assert idx["docs"]["apollo-index"]["kind"] == "router"
    assert "" not in idx["docs"]  # doc with no id omitted
    # apollo-grader is the nearest-index child of the apollo _index.
    assert idx["indexes"]["apollo-index"]["children"] == ["apollo-grader"]


def test_build_index_double_owned_picks_first_sorted():
    docs = [
        cov.Doc("docs/architecture/a.md", "a", {"description": "", "owns": ["x.py"]}, "b"),
        cov.Doc("docs/architecture/b.md", "b", {"description": "", "owns": ["x.py"]}, "b"),
    ]
    owners = {"x.py": ["b", "a"]}
    idx = cov.build_index("/repo", "docs/architecture", docs, owners, "r")
    assert idx["source_to_doc"]["x.py"] == "a"  # sorted()[0]


# --------------------------------------------------------------------------- #
# End-to-end via main(): the happy path
# --------------------------------------------------------------------------- #
def test_main_clean_repo_exits_zero(baseline_repo, capsys):
    rc = cov.main(["--repo-root", str(baseline_repo.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Summary: 0 error(s)" in out
    assert "required-universe=['.py']" in out


# --------------------------------------------------------------------------- #
# check 1: uncovered
# --------------------------------------------------------------------------- #
def test_uncovered_required_file(new_repo, capsys):
    files = baseline_files()
    files["apollo/orphan.py"] = "orphan = 1\n"  # tracked .py owned by nobody
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[ERROR] uncovered" in out
    assert "apollo/orphan.py" in out


def test_excluded_file_is_not_uncovered(new_repo, capsys):
    files = baseline_files()
    files["apollo/orphan.py"] = "orphan = 1\n"
    files["scripts/docs/owns_exclude.txt"] = "apollo/orphan.py\n"
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "uncovered" not in out.lower()


# --------------------------------------------------------------------------- #
# check 2: double-owned
# --------------------------------------------------------------------------- #
def test_double_owned(new_repo, capsys):
    files = baseline_files()
    # rag/pipeline.py is now also claimed by the apollo grader doc.
    files["docs/architecture/apollo/grader.md"] = leaf(
        "apollo-grader", owns=["apollo/grader.py", "rag/pipeline.py"], related=["rag-pipeline"]
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[ERROR] double-owned" in out
    assert "rag/pipeline.py" in out


# --------------------------------------------------------------------------- #
# check 3: dangling (exact + glob)
# --------------------------------------------------------------------------- #
def test_dangling_exact_path(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/apollo/grader.md"] = leaf(
        "apollo-grader", owns=["apollo/grader.py", "apollo/ghost.py"], related=["rag-pipeline"]
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[ERROR] dangling" in out
    assert "apollo/ghost.py" in out
    assert "matches no existing file" in out


def test_dangling_glob(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf(
        "rag-pipeline", owns=["rag/pipeline.py", "rag/none_*.py"]
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "matches no tracked file" in out


def test_glob_owns_matches_tracked_file(new_repo, capsys):
    files = baseline_files()
    files["rag/extra.py"] = "extra = 3\n"
    files["docs/architecture/rag/pipeline.md"] = leaf("rag-pipeline", owns=["rag/*.py"])
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    # rag/pipeline.py and rag/extra.py both covered by the glob -> clean.
    assert rc == 0
    assert "Summary: 0 error(s)" in out


# --------------------------------------------------------------------------- #
# check 4: owns ∩ exclude collision (HARD)
# --------------------------------------------------------------------------- #
def test_owns_exclude_collision_is_hard_error(new_repo, capsys):
    files = baseline_files()
    files["scripts/docs/owns_exclude.txt"] = "apollo/**\n"
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[ERROR] owns-exclude-collision" in out
    assert "apollo/grader.py" in out


# --------------------------------------------------------------------------- #
# check 5: frontmatter completeness
# --------------------------------------------------------------------------- #
def test_missing_doc_id(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf(
        "rag-pipeline", owns=["rag/pipeline.py"], omit=["doc"]
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "missing or empty `doc:` id" in out


def test_duplicate_doc_id(new_repo, capsys):
    files = baseline_files()
    # Give the rag pipeline leaf the same id as the apollo grader leaf.
    files["docs/architecture/rag/pipeline.md"] = leaf("apollo-grader", owns=["rag/pipeline.py"])
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "duplicate doc id 'apollo-grader'" in out


def test_missing_required_key(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf(
        "rag-pipeline", owns=["rag/pipeline.py"], omit=["stub"]
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "missing required key `stub`" in out


def test_non_router_leaf_empty_owns(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf("rag-pipeline", owns=[])
    # rag/pipeline.py is now unowned too (uncovered), but we assert the owns error.
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "non-router leaf has empty `owns`" in out


def test_stub_false_leaf_without_interface_section(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf(
        "rag-pipeline", owns=["rag/pipeline.py"], interface=False
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "has no `## Interface` section" in out


def test_stub_true_leaf_skips_interface_requirement(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf(
        "rag-pipeline", owns=["rag/pipeline.py"], stub=True, interface=False
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Summary: 0 error(s)" in out


# --------------------------------------------------------------------------- #
# check 6: related resolution
# --------------------------------------------------------------------------- #
def test_related_unresolved(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf(
        "rag-pipeline", owns=["rag/pipeline.py"], related=["does-not-exist"]
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "related id 'does-not-exist' does not resolve" in out


# --------------------------------------------------------------------------- #
# check 7: _index freshness
# --------------------------------------------------------------------------- #
def test_index_omits_child(new_repo, capsys):
    files = baseline_files()
    # apollo _index no longer references its grader child.
    files["docs/architecture/apollo/_index.md"] = router(
        "apollo-index", body="# Apollo\n\nNothing linked.\n"
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "omits child doc 'apollo-grader'" in out


def test_index_links_deleted_leaf(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/apollo/_index.md"] = router(
        "apollo-index",
        body="# Apollo\n\n- [grader](grader.md)\n- [ghost](ghost.md)\n",
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "links non-existent doc" in out
    assert "ghost.md" in out


def test_index_reference_by_doc_id(new_repo, capsys):
    files = baseline_files()
    # Reference the child by its doc id verbatim rather than a relative link,
    # and add an ignored external link to exercise the http:// skip branch.
    files["docs/architecture/apollo/_index.md"] = router(
        "apollo-index",
        body="# Apollo\n\nSee apollo-grader. Also [ext](https://example.com/x.md).\n",
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Summary: 0 error(s)" in out


def test_index_reference_by_basename_code_span(new_repo, capsys):
    files = baseline_files()
    # A code-span basename reference (`grader.md`) also satisfies the check.
    files["docs/architecture/apollo/_index.md"] = router(
        "apollo-index",
        body="# Apollo\n\nThe leaf `grader.md` documents grading.\n",
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    assert rc == 0


# --------------------------------------------------------------------------- #
# Frozen-migration sentinel
# --------------------------------------------------------------------------- #
def _migration_repo(new_repo, second_in_manifest: bool):
    files = baseline_files()
    sql_one = "create table a();\n"
    sql_two = "create table b();\n"
    files["database/migrations/001_a.sql"] = sql_one
    files["database/migrations/002_b.sql"] = sql_two
    files["docs/architecture/database/_index.md"] = router(
        "db-index", body="# DB\n\n- [migrations](migrations.md)\n"
    )
    files["docs/architecture/database/migrations.md"] = leaf(
        "db-migrations", owns=["database/migrations/"]
    )
    digest_one = hashlib.sha256(sql_one.encode()).hexdigest()
    digest_two = hashlib.sha256(sql_two.encode()).hexdigest()
    manifest = f"{digest_one}  001_a.sql\n"
    if second_in_manifest:
        manifest += f"{digest_two}  002_b.sql\n"
    else:
        manifest += "deadbeef  002_b.sql\n"  # wrong checksum
    files["database/migrations/legacy-manifest.sha256"] = manifest
    return new_repo(files)


def test_sentinel_all_migrations_covered(new_repo, capsys):
    repo = _migration_repo(new_repo, second_in_manifest=True)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Summary: 0 error(s)" in out


def test_sentinel_mismatched_migration_is_uncovered(new_repo, capsys):
    repo = _migration_repo(new_repo, second_in_manifest=False)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "002_b.sql" in out
    assert "frozen .sql not in legacy-manifest" in out


# --------------------------------------------------------------------------- #
# exists() isfile fallback (owned file present on disk but untracked)
# --------------------------------------------------------------------------- #
def test_exists_isfile_fallback_for_untracked_owned_file(new_repo):
    files = baseline_files()
    repo = new_repo(files)
    # Write an owned source file but do NOT git-add it: git ls-files won't list
    # it, so ownership resolution must fall back to os.path.isfile().
    repo.write("apollo/untracked.py", "x = 1\n")
    files_doc = leaf(
        "apollo-grader",
        owns=["apollo/grader.py", "apollo/untracked.py"],
        related=["rag-pipeline"],
    )
    repo.write("docs/architecture/apollo/grader.md", files_doc)
    # Note: intentionally NOT calling add_all() for untracked.py.
    git_root = str(repo.root)
    required = cov.detect_required_exts(git_root, None)
    report, docs, doc_ids, owners = cov.analyze(git_root, "docs/architecture", required)
    assert "apollo/untracked.py" in owners
    assert not report.errors.get("dangling")


# --------------------------------------------------------------------------- #
# --check-size wiring through main
# --------------------------------------------------------------------------- #
def test_main_check_size_flag(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf(
        "rag-pipeline", owns=["rag/pipeline.py"], body=_body(200)
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root), "--check-size"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[ERROR] size" in out
    assert "> 150" in out


# --------------------------------------------------------------------------- #
# --emit-index / --json
# --------------------------------------------------------------------------- #
def test_main_emit_index_writes_json(baseline_repo, capsys):
    rc = cov.main(
        ["--repo-root", str(baseline_repo.root), "--emit-index", "--repo-name", "hoot-be"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Wrote docs/index.json" in out
    index_path = baseline_repo.root / "docs" / "index.json"
    data = json.loads(index_path.read_text(encoding="utf-8"))
    assert data["repo"] == "hoot-be"
    assert data["source_to_doc"]["apollo/grader.py"] == "apollo-grader"
    assert data["docs"]["apollo-grader"]["kind"] == "leaf"
    assert data["$schema_version"] == 1


def test_main_json_alias_and_custom_out(baseline_repo, capsys):
    rc = cov.main(
        [
            "--repo-root",
            str(baseline_repo.root),
            "--json",
            "--index-out",
            "docs/custom-index.json",
        ]
    )
    assert rc == 0
    assert (baseline_repo.root / "docs" / "custom-index.json").exists()


# --------------------------------------------------------------------------- #
# --exit-zero advisory mode
# --------------------------------------------------------------------------- #
def test_exit_zero_downgrades_failures(new_repo, capsys):
    files = baseline_files()
    files["apollo/orphan.py"] = "x = 1\n"  # would be an uncovered error
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root), "--exit-zero"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "advisory --exit-zero" in out


def test_exit_zero_clean_repo(baseline_repo, capsys):
    rc = cov.main(["--repo-root", str(baseline_repo.root), "--exit-zero"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "advisory --exit-zero" not in out


# --------------------------------------------------------------------------- #
# --required-ext override through main
# --------------------------------------------------------------------------- #
def test_main_required_ext_override(baseline_repo, capsys):
    # Force the required universe to .ts,.tsx: no tracked .ts exist, so the .py
    # files are no longer in the required universe and nothing is uncovered.
    rc = cov.main(["--repo-root", str(baseline_repo.root), "--required-ext", "ts,tsx"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "required-universe=['.ts', '.tsx']" in out


# --------------------------------------------------------------------------- #
# last_verified PR-path mode
# --------------------------------------------------------------------------- #
def _lv_repo(new_repo):
    """Baseline repo, committed, so diffs against the first commit are possible."""
    repo = new_repo(baseline_files())
    base = repo.commit("base")
    return repo, base


def test_main_check_last_verified_without_base_warns(baseline_repo, capsys):
    rc = cov.main(["--repo-root", str(baseline_repo.root), "--check-last-verified"])
    out = capsys.readouterr().out
    assert rc == 0  # advisory warning only
    assert "skipped: no --base diff ref" in out


def test_check_last_verified_no_changes_is_silent(new_repo):
    repo, base = _lv_repo(new_repo)
    required = cov.detect_required_exts(str(repo.root), None)
    report, docs, doc_ids, owners = cov.analyze(str(repo.root), "docs/architecture", required)
    # base == HEAD -> empty diff -> nothing reported.
    cov.check_last_verified(str(repo.root), "HEAD", docs, owners, report)
    assert not report.warnings.get("last_verified")
    assert not report.errors.get("last_verified")


def test_check_last_verified_bad_base_ref_warns(new_repo):
    repo, base = _lv_repo(new_repo)
    required = cov.detect_required_exts(str(repo.root), None)
    report, docs, doc_ids, owners = cov.analyze(str(repo.root), "docs/architecture", required)
    cov.check_last_verified(str(repo.root), "no-such-ref", docs, owners, report)
    assert report.warnings.get("last_verified")
    assert "could not compute diff" in report.warnings["last_verified"][0]


def test_check_last_verified_source_changed_doc_untouched(new_repo):
    repo, base = _lv_repo(new_repo)
    # Change an owned source file without touching its owning doc.
    repo.write("apollo/grader.py", "grade = 999\n")
    repo.commit("touch source only")
    required = cov.detect_required_exts(str(repo.root), None)
    report, docs, doc_ids, owners = cov.analyze(str(repo.root), "docs/architecture", required)
    # Advisory (default) -> warning.
    cov.check_last_verified(str(repo.root), base, docs, owners, report)
    msgs = report.warnings.get("last_verified", [])
    assert any("owning leaf" in m and "not updated" in m for m in msgs)
    # Required -> hard error.
    report2 = cov.Report()
    cov.check_last_verified(str(repo.root), base, docs, owners, report2, required=True)
    assert report2.errors.get("last_verified")


def test_check_last_verified_doc_changed_but_not_bumped(new_repo):
    repo, base = _lv_repo(new_repo)
    repo.write("apollo/grader.py", "grade = 5\n")
    # Touch the doc body but NOT its last_verified line.
    repo.write(
        "docs/architecture/apollo/grader.md",
        leaf(
            "apollo-grader",
            owns=["apollo/grader.py"],
            related=["rag-pipeline"],
            body="# apollo-grader\n\n## Interface\n\nEdited body, same date.\n",
        ),
    )
    repo.commit("edit source + doc body, no lv bump")
    required = cov.detect_required_exts(str(repo.root), None)
    report, docs, doc_ids, owners = cov.analyze(str(repo.root), "docs/architecture", required)
    cov.check_last_verified(str(repo.root), base, docs, owners, report)
    msgs = report.warnings.get("last_verified", [])
    assert any("last_verified not bumped" in m for m in msgs)


def test_check_last_verified_bumped_is_clean(new_repo):
    repo, base = _lv_repo(new_repo)
    repo.write("apollo/grader.py", "grade = 7\n")
    repo.write(
        "docs/architecture/apollo/grader.md",
        leaf(
            "apollo-grader",
            owns=["apollo/grader.py"],
            related=["rag-pipeline"],
            last_verified="2026-08-01",  # bumped
        ),
    )
    repo.commit("edit source + bump lv")
    required = cov.detect_required_exts(str(repo.root), None)
    report, docs, doc_ids, owners = cov.analyze(str(repo.root), "docs/architecture", required)
    cov.check_last_verified(str(repo.root), base, docs, owners, report)
    assert not report.warnings.get("last_verified")
    assert not report.errors.get("last_verified")


def test_check_last_verified_dedup_two_sources_one_doc(new_repo):
    files = baseline_files()
    # One doc owns two source files.
    files["apollo/extra.py"] = "extra = 1\n"
    files["docs/architecture/apollo/grader.md"] = leaf(
        "apollo-grader",
        owns=["apollo/grader.py", "apollo/extra.py"],
        related=["rag-pipeline"],
    )
    repo = new_repo(files)
    base = repo.commit("base")
    # Change BOTH owned sources, leave the doc untouched.
    repo.write("apollo/grader.py", "grade = 2\n")
    repo.write("apollo/extra.py", "extra = 2\n")
    repo.commit("touch both sources")
    required = cov.detect_required_exts(str(repo.root), None)
    report, docs, doc_ids, owners = cov.analyze(str(repo.root), "docs/architecture", required)
    cov.check_last_verified(str(repo.root), base, docs, owners, report)
    msgs = report.warnings.get("last_verified", [])
    # Deduplicated: the owning doc is flagged exactly once despite two sources.
    assert len(msgs) == 1


def test_main_last_verified_required_flag_fails(new_repo, capsys):
    repo = new_repo(baseline_files())
    base = repo.commit("base")
    repo.write("apollo/grader.py", "grade = 42\n")
    repo.commit("touch source only")
    rc = cov.main(
        [
            "--repo-root",
            str(repo.root),
            "--check-last-verified",
            "--last-verified-required",
            "--base",
            base,
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "[ERROR] last_verified" in out


def test_main_last_verified_advisory_does_not_fail(new_repo, capsys):
    repo = new_repo(baseline_files())
    base = repo.commit("base")
    repo.write("apollo/grader.py", "grade = 43\n")
    repo.commit("touch source only")
    rc = cov.main(["--repo-root", str(repo.root), "--check-last-verified", "--base", base])
    out = capsys.readouterr().out
    assert rc == 0  # advisory: warning, not failure
    assert "[warn] last_verified" in out


# --------------------------------------------------------------------------- #
# UTF-8 arrow regression (cp1252 crash fix): a non-ASCII char in a diffed doc
# must not crash the last_verified git-decode path.
# --------------------------------------------------------------------------- #
def test_last_verified_handles_utf8_arrow_in_doc(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/apollo/grader.md"] = leaf(
        "apollo-grader",
        owns=["apollo/grader.py"],
        related=["rag-pipeline"],
        body="# apollo-grader\n\n## Interface\n\nFlow: input → grade.\n",
    )
    repo = new_repo(files)
    base = repo.commit("base with arrow")
    # Edit the source and the doc body (keeping the arrow), no lv bump.
    repo.write("apollo/grader.py", "grade = 8\n")
    files["docs/architecture/apollo/grader.md"] = leaf(
        "apollo-grader",
        owns=["apollo/grader.py"],
        related=["rag-pipeline"],
        body="# apollo-grader\n\n## Interface\n\nFlow: input → grade (rev).\n",
    )
    repo.write(
        "docs/architecture/apollo/grader.md",
        files["docs/architecture/apollo/grader.md"],
    )
    repo.commit("edit arrow doc + source")
    rc = cov.main(["--repo-root", str(repo.root), "--check-last-verified", "--base", base])
    out = capsys.readouterr().out
    # The point is: no UnicodeDecodeError; the run completes and warns.
    assert rc == 0
    assert "last_verified not bumped" in out


# --------------------------------------------------------------------------- #
# default repo_name derivation (no --repo-name given)
# --------------------------------------------------------------------------- #
def test_default_repo_name_is_dir_basename(baseline_repo, capsys):
    cov.main(["--repo-root", str(baseline_repo.root)])
    out = capsys.readouterr().out
    assert f"repo={os.path.basename(str(baseline_repo.root))}" in out


# --------------------------------------------------------------------------- #
# add_owner dedup: the same doc owning a file via BOTH a glob and an exact path
# must record the owner only once (no double-owned false positive).
# --------------------------------------------------------------------------- #
def test_same_doc_owns_file_via_glob_and_exact(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/rag/pipeline.md"] = leaf(
        "rag-pipeline", owns=["rag/*.py", "rag/pipeline.py"]
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "double-owned" not in out.lower()


# --------------------------------------------------------------------------- #
# _index freshness: a link to the index doc ITSELF is skipped, not treated as a
# deleted-leaf row.
# --------------------------------------------------------------------------- #
def test_index_self_link_is_skipped(new_repo, capsys):
    files = baseline_files()
    files["docs/architecture/apollo/_index.md"] = router(
        "apollo-index",
        body="# Apollo\n\n- [grader](grader.md)\n- [self](_index.md)\n",
    )
    repo = new_repo(files)
    rc = cov.main(["--repo-root", str(repo.root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Summary: 0 error(s)" in out


# --------------------------------------------------------------------------- #
# check_last_verified: the inner per-doc `git diff` raising is swallowed and the
# doc is still flagged (dd == "" -> no +last_verified match).
# --------------------------------------------------------------------------- #
def test_last_verified_inner_diff_exception(new_repo, monkeypatch):
    from types import SimpleNamespace

    repo, base = _lv_repo(new_repo)
    required = cov.detect_required_exts(str(repo.root), None)
    report, docs, doc_ids, owners = cov.analyze(str(repo.root), "docs/architecture", required)

    calls = {"n": 0}
    first_out = "apollo/grader.py\ndocs/architecture/apollo/grader.md\n"

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(stdout=first_out)  # name-only diff succeeds
        raise RuntimeError("per-doc diff blew up")  # inner diff raises

    monkeypatch.setattr(cov.subprocess, "run", fake_run)
    cov.check_last_verified(str(repo.root), base, docs, owners, report)
    msgs = report.warnings.get("last_verified", [])
    assert any("last_verified not bumped" in m for m in msgs)


# --------------------------------------------------------------------------- #
# main(): stdout/stderr without a .reconfigure() method must not crash (the
# reconfigure guard swallows AttributeError).
# --------------------------------------------------------------------------- #
def test_main_reconfigure_guard_handles_missing_method(baseline_repo, monkeypatch):
    import io
    import sys

    buf_out = io.StringIO()  # StringIO has no .reconfigure -> guard swallows it
    buf_err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf_out)
    monkeypatch.setattr(sys, "stderr", buf_err)
    rc = cov.main(["--repo-root", str(baseline_repo.root)])
    assert rc == 0
    assert "Summary: 0 error(s)" in buf_out.getvalue()
