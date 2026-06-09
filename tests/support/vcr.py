"""VCR.py configuration for replaying LLM / HTTP interactions in integration tests.

Records real OpenAI (and other HTTP) calls to YAML cassettes once, then replays
them deterministically — so integration tests exercise the real request/response
shapes without ever hitting the network in CI.

Safety rails:
  - Secrets (`authorization`, `x-api-key`, ...) are scrubbed from cassettes.
  - In CI (`CI` env set) the record mode is forced to ``none`` — a missing
    cassette fails loudly instead of silently calling the live API.
  - Locally, set ``VCR_RECORD_MODE=once`` to record new cassettes.

Usage:

    from tests.support.vcr import use_cassette

    async def test_answer(async_openai_call):
        with use_cassette("openai_answer"):
            ... # the wrapped HTTP call replays from cassettes/openai_answer.yaml
"""
from __future__ import annotations

import os
from pathlib import Path

import vcr

CASSETTE_DIR = Path(__file__).resolve().parents[1] / "cassettes"

# Headers that must never be persisted to a cassette.
FILTER_HEADERS = [
    "authorization",
    "x-api-key",
    "api-key",
    "openai-organization",
    "openai-project",
    "cookie",
    "set-cookie",
]


def record_mode() -> str:
    """Resolve the VCR record mode. ``none`` (replay-only) in CI."""
    if os.getenv("CI"):
        return "none"
    return os.getenv("VCR_RECORD_MODE", "none")


def build_vcr() -> vcr.VCR:
    """A VCR instance with secret scrubbing and body-sensitive matching."""
    return vcr.VCR(
        cassette_library_dir=str(CASSETTE_DIR),
        record_mode=record_mode(),
        filter_headers=FILTER_HEADERS,
        filter_post_data_parameters=["api_key"],
        # Match on body so different LLM prompts map to different cassettes.
        match_on=["method", "scheme", "host", "port", "path", "query", "body"],
        decode_compressed_response=True,
    )


def use_cassette(name: str):
    """Context manager that replays/records ``cassettes/<name>.yaml``."""
    CASSETTE_DIR.mkdir(exist_ok=True)
    return build_vcr().use_cassette(f"{name}.yaml")
