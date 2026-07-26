#!/usr/bin/env python3
"""Architecture-doc ownership bijection lint (docs-restructure enforcement).

Deterministic, stdlib-first (PyYAML used when present, tiny built-in fallback
otherwise), no network / no Docker, runs in well under a second. The SAME script
runs against the backend and both UI repos via ``--repo-root`` / ``--docs-root``.

What it enforces (PLAN §6.1):

  1. Uncovered      — a required-universe source file owned by 0 leaves.
  2. Double-owned   — any file (required or optional) owned by >1 leaf.
  3. Dangling       — an ``owns`` path that matches no existing file.
  4. owns∩exclude   — a path in BOTH an ``owns`` and an exclude pattern (HARD error;
                      never a silent drop).
  5. Frontmatter    — missing required key, non-router leaf with empty ``owns``,
                      a ``stub:false`` leaf whose body has no ``## Interface`` section,
                      or a duplicate ``doc:`` id.
  6. related-resolve — every ``related:`` id resolves to an existing ``doc:`` id.
  7. _index freshness — every ``_index.md`` lists exactly the child docs (leaves +
                      sub-domain indexes) that live under it.
  8. last_verified  — (PR path, ``--check-last-verified`` + ``--base``) a changed owned
                      source file whose owning leaf's ``last_verified`` did not change.
                      Advisory unless ``--last-verified-required``.

Extra modes:
  --check-size   leaf body >150 or _index/_overview body >60 → error; leaf >80 → warn.
  --emit-index   (re)generate ``docs/index.json`` from doc frontmatter (source→doc map,
                 doc manifest, and per-index child tables). ``--json`` is an alias.

Frozen-migration sentinel (PLAN §2.4, the ONLY glob-like ``owns`` exception):
  A doc may declare the single token ``database/migrations/``. Each
  ``database/migrations/*.sql`` is treated as owned by that doc ONLY when its
  LF-normalized SHA-256 is listed in ``database/migrations/legacy-manifest.sha256``.
  A new/renamed/edited .sql not matching the manifest is reported UNCOVERED — the
  alarm is preserved exactly where an unreviewed migration is most dangerous.

Exit code: 0 when no hard finding, 1 otherwise. (Advisory findings never fail.)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date

# ---------------------------------------------------------------------------
# Frontmatter parsing (PyYAML if available; tiny deterministic fallback else).
# ---------------------------------------------------------------------------

try:  # pragma: no cover - trivial import guard
    import yaml  # type: ignore

    _HAVE_YAML = True
except Exception:  # pragma: no cover
    yaml = None  # type: ignore
    _HAVE_YAML = False


def _fallback_parse(text: str) -> dict:
    """Parse the small YAML subset our frontmatter uses (scalars + string lists).

    Handles ``key: value``, ``key:`` followed by ``  - item`` blocks, and inline
    ``key: [a, b]`` lists. Good enough for the controlled frontmatter contract; the
    full PyYAML path is preferred when the module is installed.
    """
    data: dict = {}
    lines = text.splitlines()
    i = 0
    key_re = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")
    item_re = re.compile(r"^\s*-\s*(.*)$")
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        m = key_re.match(raw)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "" or val is None:
            # Possibly a block list on following indented '- ' lines.
            items = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() == "" or nxt.lstrip().startswith("#"):
                    j += 1
                    continue
                im = item_re.match(nxt)
                if im and (len(nxt) - len(nxt.lstrip())) > 0:
                    items.append(_strip_scalar(im.group(1)))
                    j += 1
                    continue
                break
            data[key] = items if items else ""
            i = j
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [_strip_scalar(x) for x in inner.split(",") if x.strip()] if inner else []
        else:
            data[key] = _coerce_scalar(_strip_scalar(val))
        i += 1
    return data


def _strip_scalar(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _coerce_scalar(s):
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    return s


def parse_frontmatter(text: str):
    """Return (frontmatter_dict_or_None, body_str). None dict = no/invalid block."""
    if not text.startswith("---"):
        return None, text
    # Find the closing delimiter line.
    lines = text.splitlines(keepends=True)
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == "---":
            end = idx
            break
    if end is None:
        return None, text
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    try:
        if _HAVE_YAML:
            data = yaml.safe_load(fm_text)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                return None, body
        else:
            data = _fallback_parse(fm_text)
    except Exception:
        return None, body
    return data, body


# ---------------------------------------------------------------------------
# Glob matching (path-aware; ``**`` spans directories, ``*`` does not cross ``/``).
# ---------------------------------------------------------------------------

_glob_cache: dict = {}


def compile_glob(pattern: str) -> re.Pattern:
    rx = _glob_cache.get(pattern)
    if rx is not None:
        return rx
    escaped = re.escape(pattern)
    # Undo escaping of glob metacharacters, translating (longest first).
    escaped = escaped.replace(r"\*\*/", "(?:.*/)?")
    escaped = escaped.replace(r"\*\*", ".*")
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = escaped.replace(r"\?", "[^/]")
    rx = re.compile("^" + escaped + "$")
    _glob_cache[pattern] = rx
    return rx


def is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


def glob_match(path: str, pattern: str) -> bool:
    return compile_glob(pattern).match(path) is not None


# ---------------------------------------------------------------------------
# Repo / doc discovery.
# ---------------------------------------------------------------------------

SENTINEL_MIGRATIONS = "database/migrations/"
LEGACY_MANIFEST = "database/migrations/legacy-manifest.sha256"
ROUTER_BASENAMES = ("_index.md", "_overview.md")
REQUIRED_KEYS = ("doc", "description", "owns", "related", "last_verified", "stub")


def git_tracked_files(repo_root: str) -> list:
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "ls-files"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except Exception as exc:  # pragma: no cover - environment guard
        print(f"ERROR: could not run `git ls-files` in {repo_root}: {exc}", file=sys.stderr)
        return []
    files = [ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()]
    return files


def detect_required_exts(repo_root: str, override: str | None) -> set:
    if override:
        return {e if e.startswith(".") else "." + e for e in override.split(",")}
    if os.path.exists(os.path.join(repo_root, "requirements.txt")) or os.path.exists(
        os.path.join(repo_root, "pytest.ini")
    ):
        return {".py"}
    if os.path.exists(os.path.join(repo_root, "tsconfig.json")):
        return {".ts", ".tsx"}
    # Fallback: infer from what is present.
    return {".py"}


def load_excludes(repo_root: str) -> list:
    path = os.path.join(repo_root, "scripts", "docs", "owns_exclude.txt")
    patterns = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                patterns.append(ln.replace("\\", "/"))
    return patterns


def excluded(path: str, patterns: list) -> bool:
    return any(glob_match(path, p) for p in patterns)


class Doc:
    __slots__ = ("relpath", "doc_id", "fm", "body", "owns", "related", "is_router", "stub")

    def __init__(self, relpath, doc_id, fm, body):
        self.relpath = relpath  # repo-relative md path (posix)
        self.doc_id = doc_id
        self.fm = fm
        self.body = body
        owns = fm.get("owns") if isinstance(fm, dict) else None
        self.owns = [str(o).replace("\\", "/") for o in owns] if isinstance(owns, list) else []
        related = fm.get("related") if isinstance(fm, dict) else None
        self.related = [str(r) for r in related] if isinstance(related, list) else []
        self.is_router = os.path.basename(relpath) in ROUTER_BASENAMES
        self.stub = bool(fm.get("stub")) if isinstance(fm, dict) else False


def load_docs(repo_root: str, docs_root: str):
    docs = []
    abs_docs = os.path.join(repo_root, docs_root)
    for dirpath, _dirs, filenames in os.walk(abs_docs):
        for fn in sorted(filenames):
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, repo_root).replace("\\", "/")
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
            fm, body = parse_frontmatter(text)
            doc_id = ""
            if isinstance(fm, dict) and fm.get("doc"):
                doc_id = str(fm["doc"]).strip()
            docs.append(Doc(rel, doc_id, fm if isinstance(fm, dict) else {}, body))
    docs.sort(key=lambda d: d.relpath)
    return docs


# ---------------------------------------------------------------------------
# Frozen-migration manifest.
# ---------------------------------------------------------------------------


def load_manifest(repo_root: str) -> dict[str, str]:
    """basename -> expected sha256 hex, parsed from legacy-manifest.sha256."""
    path = os.path.join(repo_root, LEGACY_MANIFEST)
    manifest: dict[str, str] = {}
    if not os.path.exists(path):
        return manifest
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split()
            if len(parts) < 2:
                continue
            digest = parts[0]
            name = parts[-1].lstrip("*").replace("\\", "/")
            manifest[os.path.basename(name)] = digest.lower()
    return manifest


def lf_sha256(path: str) -> str:
    with open(path, "rb") as fh:
        data = fh.read()
    data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Core analysis.
# ---------------------------------------------------------------------------


class Report:
    def __init__(self):
        self.errors = {}  # category -> list[str]
        self.warnings = {}  # category -> list[str]

    def err(self, cat, msg):
        self.errors.setdefault(cat, []).append(msg)

    def warn(self, cat, msg):
        self.warnings.setdefault(cat, []).append(msg)

    def has_errors(self):
        return any(self.errors.values())


def analyze(repo_root, docs_root, required_exts, cap=200):
    report = Report()
    tracked = git_tracked_files(repo_root)
    tracked_set = set(tracked)
    excludes = load_excludes(repo_root)
    docs = load_docs(repo_root, docs_root)

    def exists(rel):
        return rel in tracked_set or os.path.isfile(os.path.join(repo_root, rel))

    # --- doc id table + duplicate detection + frontmatter completeness ---
    doc_ids = {}
    for d in docs:
        if not d.doc_id:
            report.err("frontmatter", f"{d.relpath}: missing or empty `doc:` id")
            continue
        if d.doc_id in doc_ids:
            report.err(
                "frontmatter",
                f"duplicate doc id '{d.doc_id}' in {d.relpath} and {doc_ids[d.doc_id].relpath}",
            )
        else:
            doc_ids[d.doc_id] = d

    for d in docs:
        for key in REQUIRED_KEYS:
            if key not in d.fm:
                report.err("frontmatter", f"{d.relpath}: missing required key `{key}`")
        if not d.is_router and not d.owns:
            report.err("frontmatter", f"{d.relpath}: non-router leaf has empty `owns`")
        if not d.is_router and not d.stub and "## Interface" not in d.body:
            report.err("frontmatter", f"{d.relpath}: stub:false leaf has no `## Interface` section")

    # --- ownership map (file -> [doc_id, ...]) ---
    owners = {}  # rel path -> list of doc ids
    owned_via = {}  # rel path -> owning doc id -> "exact"|"glob"|"sentinel"
    manifest = load_manifest(repo_root)

    def add_owner(rel, doc_id, how):
        owners.setdefault(rel, [])
        if doc_id not in owners[rel]:
            owners[rel].append(doc_id)
        owned_via.setdefault(rel, {})[doc_id] = how

    for d in docs:
        for entry in d.owns:
            if entry == SENTINEL_MIGRATIONS:
                # Frozen-migration sentinel: cover only manifest-matching *.sql.
                for rel in tracked:
                    if rel.startswith(SENTINEL_MIGRATIONS) and rel.endswith(".sql"):
                        base = os.path.basename(rel)
                        expected = manifest.get(base)
                        actual = lf_sha256(os.path.join(repo_root, rel)) if exists(rel) else None
                        if expected and actual == expected:
                            add_owner(rel, d.doc_id, "sentinel")
                        # else: intentionally left uncovered (frozen alarm).
                continue
            if is_glob(entry):
                matched = [rel for rel in tracked if glob_match(rel, entry)]
                if not matched:
                    report.err(
                        "dangling",
                        f"{d.doc_id or d.relpath}: owns glob '{entry}' matches no tracked file",
                    )
                for rel in matched:
                    add_owner(rel, d.doc_id, "glob")
            else:
                if not exists(entry):
                    report.err(
                        "dangling",
                        f"{d.doc_id or d.relpath}: owns path '{entry}' matches no existing file",
                    )
                else:
                    add_owner(entry, d.doc_id, "exact")

    # --- check 2: double-owned ---
    for rel in sorted(owners):
        if len(owners[rel]) > 1:
            report.err("double-owned", f"{rel}: owned by {sorted(owners[rel])}")

    # --- check 4: owns ∩ exclude collision (hard) ---
    for rel in sorted(owners):
        if excluded(rel, excludes):
            report.err(
                "owns-exclude-collision",
                f"{rel}: owned by {sorted(owners[rel])} but also matches an exclude pattern",
            )

    # --- check 1: uncovered (required universe) ---
    def in_required(rel):
        _, ext = os.path.splitext(rel)
        return ext in required_exts

    for rel in tracked:
        if not in_required(rel):
            continue
        if excluded(rel, excludes):
            continue
        if rel not in owners:
            report.err("uncovered", rel)

    # Frozen-migration extra alarm: a *.sql under the sentinel dir that no doc
    # covers because it is missing from / mismatched against the manifest.
    sentinel_declared = any(SENTINEL_MIGRATIONS in d.owns for d in docs)
    if sentinel_declared:
        for rel in tracked:
            if rel.startswith(SENTINEL_MIGRATIONS) and rel.endswith(".sql") and rel not in owners:
                report.err(
                    "uncovered",
                    f"{rel}: frozen .sql not in legacy-manifest.sha256 (or checksum mismatch)",
                )

    # --- check 6: related resolution ---
    all_ids = set(doc_ids)
    for d in docs:
        for rid in d.related:
            if rid not in all_ids:
                report.err(
                    "related-unresolved",
                    f"{d.doc_id or d.relpath}: related id '{rid}' does not resolve",
                )

    # --- check 7: _index freshness ---
    _index_freshness(repo_root, docs_root, docs, doc_ids, report)

    return report, docs, doc_ids, owners


def _doc_dir(relpath: str) -> str:
    return os.path.dirname(relpath).replace("\\", "/")


def _nearest_index_dir(target_dir: str, index_dirs: set, self_is_index: bool) -> str | None:
    """Deepest index directory that is an ancestor of target_dir.

    For an index doc itself, its parent index must be a STRICT ancestor.
    """
    parts = target_dir.split("/") if target_dir else []
    best = None
    for i in range(len(parts), -1, -1):
        cand = "/".join(parts[:i])
        if cand in index_dirs:
            if self_is_index and cand == target_dir:
                continue
            best = cand
            break
    return best


def _index_freshness(repo_root, docs_root, docs, doc_ids, report):
    """Each _index must reference exactly its child docs (leaves + sub-indexes).

    'child' = a doc whose nearest ancestor _index is this one. A child may be
    referenced by a relative markdown link/path OR by its doc id verbatim. Two
    drift directions are flagged:
      - omitted new leaf: an expected child not referenced anywhere in the body.
      - deleted-leaf row : a relative .md link that resolves under this index's
        subtree but points at a doc that does not exist.
    """
    index_docs = [d for d in docs if os.path.basename(d.relpath) == "_index.md"]
    index_dirs = {_doc_dir(d.relpath) for d in index_docs}
    all_relpaths = {d.relpath for d in docs}

    expected = {idx_dir: [] for idx_dir in index_dirs}
    for d in docs:
        base = os.path.basename(d.relpath)
        if base == "_overview.md":
            continue  # root router is not an _index
        parent = _nearest_index_dir(_doc_dir(d.relpath), index_dirs, base == "_index.md")
        if parent is not None:
            expected[parent].append(d)

    link_re = re.compile(r"\]\(([^)]+?\.md)\)")
    code_re = re.compile(r"`([^`]+?\.md)`")

    for idx in index_docs:
        idx_dir = _doc_dir(idx.relpath)
        children = expected.get(idx_dir, [])
        body = idx.body

        # Referenced relative .md targets resolved to repo-relative paths.
        raw_targets = set(link_re.findall(body)) | set(code_re.findall(body))
        resolved = set()
        for t in raw_targets:
            t = t.split("#", 1)[0].strip()
            if not t or t.startswith(("http://", "https://")):
                continue
            rp = os.path.normpath(os.path.join(idx_dir, t)).replace("\\", "/")
            resolved.add(rp)

        for c in children:
            referenced = (
                c.relpath in resolved
                or (c.doc_id and c.doc_id in body)
                or os.path.basename(c.relpath) in {os.path.basename(r) for r in resolved}
            )
            if not referenced:
                report.err(
                    "index-freshness",
                    f"{idx.relpath}: omits child doc '{c.doc_id or c.relpath}'",
                )

        # Deleted-leaf rows: a link under this index's subtree to a missing doc.
        for rp in sorted(resolved):
            if rp == idx.relpath:
                continue
            if rp.startswith(idx_dir + "/") and rp not in all_relpaths:
                report.err(
                    "index-freshness",
                    f"{idx.relpath}: links non-existent doc '{rp}' (deleted-leaf row?)",
                )


# ---------------------------------------------------------------------------
# Size checks.
# ---------------------------------------------------------------------------


def check_size(docs, report):
    for d in docs:
        body_lines = [ln for ln in d.body.splitlines()]
        n = len(body_lines)
        base = os.path.basename(d.relpath)
        if base in ROUTER_BASENAMES:
            if n > 60:
                report.err("size", f"{d.relpath}: router body {n} lines > 60")
        else:
            if n > 150:
                report.err("size", f"{d.relpath}: leaf body {n} lines > 150 (hard cap)")
            elif n > 80:
                report.warn("size", f"{d.relpath}: leaf body {n} lines > 80 (soft target)")


# ---------------------------------------------------------------------------
# last_verified PR-path check.
# ---------------------------------------------------------------------------


def check_last_verified(repo_root, base, docs, owners, report, required=False):
    try:
        out = subprocess.run(
            ["git", "-C", repo_root, "diff", "--name-only", f"{base}...HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except Exception as exc:
        report.warn("last_verified", f"could not compute diff vs {base}: {exc}")
        return
    changed = {ln.strip().replace("\\", "/") for ln in out.splitlines() if ln.strip()}
    if not changed:
        return
    id_to_doc = {d.doc_id: d for d in docs if d.doc_id}
    # For each changed owned source file, its owning doc must also be changed with
    # a last_verified delta.
    flagged = set()
    for rel in sorted(changed):
        for doc_id in owners.get(rel, []):
            doc = id_to_doc.get(doc_id)
            if not doc or doc_id in flagged:
                continue
            if doc.relpath not in changed:
                _emit_lv(
                    report, required, f"{rel} changed but owning leaf {doc.relpath} not updated"
                )
                flagged.add(doc_id)
                continue
            # Owning doc changed — verify last_verified actually moved.
            try:
                dd = subprocess.run(
                    ["git", "-C", repo_root, "diff", f"{base}...HEAD", "--", doc.relpath],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=True,
                ).stdout
            except Exception:
                dd = ""
            if not re.search(r"^\+.*last_verified", dd, re.MULTILINE):
                _emit_lv(
                    report,
                    required,
                    f"{rel} changed but {doc.relpath} last_verified not bumped",
                )
                flagged.add(doc_id)


def _emit_lv(report, required, msg):
    if required:
        report.err("last_verified", msg)
    else:
        report.warn("last_verified", msg)


# ---------------------------------------------------------------------------
# index.json emission (from real frontmatter).
# ---------------------------------------------------------------------------


def build_index(repo_root, docs_root, docs, owners, repo_name):
    source_to_doc = {}
    for rel in sorted(owners):
        # a source file maps to its (single) owner; on double-owns, list is >1 —
        # pick the first deterministically and let the double-owned check fail.
        source_to_doc[rel] = sorted(owners[rel])[0]
    docs_map = {}
    for d in sorted(docs, key=lambda x: x.doc_id or x.relpath):
        if not d.doc_id:
            continue
        docs_map[d.doc_id] = {
            "path": d.relpath,
            "kind": "router" if d.is_router else "leaf",
            "description": str(d.fm.get("description", "")).strip(),
            "owns": list(d.owns),
        }
    # index children (nearest-index nesting).
    index_docs = [d for d in docs if os.path.basename(d.relpath) == "_index.md"]
    index_dirs = {_doc_dir(d.relpath) for d in index_docs}
    indexes = {}
    for idx in index_docs:
        indexes[idx.doc_id or idx.relpath] = {"path": idx.relpath, "children": []}
    for d in docs:
        base = os.path.basename(d.relpath)
        if base == "_overview.md":
            continue
        parent = _nearest_index_dir(_doc_dir(d.relpath), index_dirs, base == "_index.md")
        if parent is None:
            continue
        for idx in index_docs:
            if _doc_dir(idx.relpath) == parent:
                indexes[idx.doc_id or idx.relpath]["children"].append(d.doc_id or d.relpath)
    for v in indexes.values():
        v["children"] = sorted(v["children"])
    return {
        "$schema_version": 1,
        "generated_by": "check_owns_coverage.py --emit-index",
        "generated_at": date.today().isoformat(),
        "repo": repo_name,
        "docs_root": docs_root,
        "source_to_doc": source_to_doc,
        "docs": docs_map,
        "indexes": indexes,
    }


# ---------------------------------------------------------------------------
# Output.
# ---------------------------------------------------------------------------


def print_report(report, cap):
    order = [
        "frontmatter",
        "uncovered",
        "double-owned",
        "dangling",
        "owns-exclude-collision",
        "related-unresolved",
        "index-freshness",
        "size",
        "last_verified",
    ]
    seen = set()
    total_err = 0
    for cat in order + sorted(set(report.errors) - set(order)):
        if cat in seen:
            continue
        seen.add(cat)
        items = report.errors.get(cat, [])
        if not items:
            continue
        total_err += len(items)
        print(f"\n[ERROR] {cat}: {len(items)} finding(s)")
        for msg in sorted(items)[:cap]:
            print(f"  - {msg}")
        if len(items) > cap:
            print(f"  … {len(items) - cap} more (capped at {cap})")
    total_warn = 0
    for cat in sorted(report.warnings):
        items = report.warnings.get(cat, [])
        if not items:
            continue
        total_warn += len(items)
        print(f"\n[warn] {cat}: {len(items)} finding(s)")
        for msg in sorted(items)[:cap]:
            print(f"  - {msg}")
        if len(items) > cap:
            print(f"  … {len(items) - cap} more (capped at {cap})")
    print(f"\nSummary: {total_err} error(s), {total_warn} warning(s).")
    return total_err


def main(argv=None):
    ap = argparse.ArgumentParser(description="Architecture-doc ownership bijection lint.")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--docs-root", default="docs/architecture")
    ap.add_argument(
        "--required-ext", default=None, help="override required universe, e.g. 'py' or 'ts,tsx'"
    )
    ap.add_argument(
        "--repo-name", default=None, help="name recorded in index.json (default: repo dir basename)"
    )
    ap.add_argument("--check-size", action="store_true")
    ap.add_argument(
        "--check-last-verified", action="store_true", help="PR-path last_verified check (advisory)"
    )
    ap.add_argument(
        "--last-verified-required",
        action="store_true",
        help="make the last_verified check fail the run",
    )
    ap.add_argument("--base", default=None, help="diff base ref for the PR-path check")
    ap.add_argument(
        "--emit-index", action="store_true", help="regenerate docs/index.json from frontmatter"
    )
    ap.add_argument("--json", action="store_true", help="alias for --emit-index")
    ap.add_argument("--index-out", default="docs/index.json")
    ap.add_argument("--cap", type=int, default=200, help="max findings printed per category")
    ap.add_argument(
        "--exit-zero",
        action="store_true",
        help="always exit 0 (advisory: report findings without failing; used by the pre-commit hook during the split)",
    )
    args = ap.parse_args(argv)

    # Windows consoles default to a legacy codepage; keep output ASCII-safe so a
    # non-ASCII path or description never crashes the lint (CI is already UTF-8).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    repo_root = os.path.abspath(args.repo_root)
    repo_name = args.repo_name or os.path.basename(repo_root.rstrip("/\\"))
    required_exts = detect_required_exts(repo_root, args.required_ext)

    report, docs, doc_ids, owners = analyze(repo_root, args.docs_root, required_exts, cap=args.cap)

    if args.check_size:
        check_size(docs, report)

    if args.check_last_verified:
        if args.base:
            check_last_verified(
                repo_root, args.base, docs, owners, report, required=args.last_verified_required
            )
        else:
            report.warn("last_verified", "skipped: no --base diff ref provided")

    if args.emit_index or args.json:
        index = build_index(repo_root, args.docs_root, docs, owners, repo_name)
        out_path = os.path.join(repo_root, args.index_out)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(index, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print(
            f"Wrote {args.index_out} "
            f"({len(index['source_to_doc'])} source->doc mappings, {len(index['docs'])} docs)."
        )

    print(f"repo={repo_name} required-universe={sorted(required_exts)} docs={len(docs)}")
    total_err = print_report(report, args.cap)
    if args.exit_zero:
        if total_err:
            print("(advisory --exit-zero: findings above are NOT failing this run)")
        return 0
    return 1 if total_err > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
