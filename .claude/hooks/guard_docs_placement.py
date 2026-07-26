#!/usr/bin/env python3
"""PreToolUse guard (non-blocking): warn when a NEW doc .md is written outside
the allowed locations. Part of the docs _archive/ quarantine convention.
Always exits 0 — this only nudges, it never blocks."""

import json
import os
import sys

ALLOWED = ("/architecture/", "/shared-architecture/", "/_archive/", "/docs/superpowers/")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") != "Write":
        return 0
    raw = (data.get("tool_input") or {}).get("file_path", "") or ""
    path = raw.replace("\\", "/")
    low = path.lower()
    if "/docs/" not in low or not low.endswith(".md"):
        return 0
    if os.path.exists(path):  # editing an existing file is exempt
        return 0
    if any(seg in path for seg in ALLOWED):
        return 0
    sys.stderr.write(
        f"WARNING docs-placement: new doc '{raw}' is outside the durable tree.\n"
        "  Transient docs (handoffs/plans/specs/experiments) belong in the owning\n"
        "  repo's docs/_archive/. Durable docs go in architecture/ or\n"
        "  shared-architecture/. See CLAUDE.md 'Markdown placement'.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
