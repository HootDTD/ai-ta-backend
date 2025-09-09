#!/usr/bin/env python3
"""
Save *all* files from a directory tree into a single text file
as nicely-formatted Markdown code blocks, while excluding a specific path.
"""

import os
import sys
from datetime import datetime

# ─── USER SETTINGS ────────────────────────────────────────────────────────────
ROOT_DIR = r"C:\Users\ultra\OneDrive\TA-test\AI-TA"

# Exclude this exact subtree (recursively)
EXCLUDE_PATHS = {
    os.path.normcase(os.path.normpath(
        r"C:\Users\ultra\OneDrive\TA-test\AI-TA\backend\text-embeder\my_book_index_aero_smoke"
    ))
}

# Folder & file name filters
EXCLUDE_DIRS_BY_NAME = {"node_modules", ".git", ".idea", "__pycache__", "dist", "build"}
EXCLUDE_FILES_BY_NAME = {".DS_Store"}

# Save file directly to your Downloads folder
DOWNLOADS_DIR = r"C:\Users\ultra\Downloads"
OUTPUT_FILE = os.path.join(
    DOWNLOADS_DIR,
    f"snapshot_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
)
# ──────────────────────────────────────────────────────────────────────────────

def is_under_any_excluded(path: str) -> bool:
    """Return True if `path` is inside any path in EXCLUDE_PATHS."""
    npath = os.path.normcase(os.path.normpath(path))
    for ex in EXCLUDE_PATHS:
        # If ex is a prefix ancestor of npath, commonpath will equal ex
        try:
            if os.path.commonpath([npath, ex]) == ex:
                return True
        except ValueError:
            # Different drives on Windows -> can't be ancestor
            pass
    return False

def gather_tree(root: str) -> str:
    """Walk `root`, collect every file’s content in Markdown blocks."""
    if not os.path.isdir(root):
        sys.exit(f"❌ Directory not found: {root}")

    md_chunks = [f"# Snapshot of `{root}`\n"]
    file_count = 0

    for cur_root, dirs, files in os.walk(root):
        # 1) If current root is inside an excluded subtree, skip entirely
        if is_under_any_excluded(cur_root):
            # Do not descend further from this point
            dirs[:] = []
            continue

        # 2) Prune directories BEFORE descending (both by name and by path)
        pruned = []
        for d in list(dirs):
            if d in EXCLUDE_DIRS_BY_NAME:
                continue
            candidate = os.path.join(cur_root, d)
            if is_under_any_excluded(candidate):
                # prevent descent into excluded subtree
                continue
            pruned.append(d)
        dirs[:] = pruned

        # 3) Process files at this level
        for name in files:
            if name in EXCLUDE_FILES_BY_NAME:
                continue

            path = os.path.join(cur_root, name)
            if is_under_any_excluded(path):
                continue

            rel = os.path.relpath(path, root)

            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    data = fh.read()
            except (OSError, IOError) as e:
                print(f"⚠️  Skipping {rel}: {e}")
                continue

            _, ext = os.path.splitext(name)
            lang = ext.lstrip(".") or ""  # empty string → plain text
            md_chunks.append(f"## {rel}\n```{lang}\n{data}\n```\n")
            file_count += 1

    print(f"✅ Processed {file_count} files from {root}.")
    return "\n".join(md_chunks)

def main() -> None:
    output_chunk = gather_tree(ROOT_DIR)

    if not output_chunk.strip():
        sys.exit("Nothing collected – aborting.")

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(output_chunk)

    print(f"💾 Output saved to: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
