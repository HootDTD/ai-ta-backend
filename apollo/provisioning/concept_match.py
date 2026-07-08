"""Closed-list concept matching (reversed provisioning).

The course carries a PREMADE concept list; each scraped problem is CLASSIFIED
against it instead of minting new concepts. This module owns the matcher and
the slug-normalization convention shared with the premade-list seeder.
"""

from __future__ import annotations

__all__ = ["norm_slug"]


def norm_slug(slug: str) -> str:
    """Hyphen/underscore/case-insensitive slug key.

    The premade-list seeder and the matcher share this so a hyphenated list
    slug (``integration-by-parts``) matches a registry-seeded underscore row
    (``integration_by_parts``) instead of duplicating it.
    """
    return slug.strip().lower().replace("-", "_")
