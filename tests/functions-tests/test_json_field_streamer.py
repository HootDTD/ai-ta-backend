"""Tests for incremental extraction of a JSON string field from a growing buffer."""
from __future__ import annotations

import json

import pytest

from ai.streaming import JsonStringFieldStreamer


def _drain(chunks, field="steps"):
    """Feed chunks; return (concatenated_emitted_text, complete_flag)."""
    s = JsonStringFieldStreamer(field=field)
    out = []
    for c in chunks:
        out.append(s.feed(c))
    return "".join(out), s.complete


def test_simple_single_chunk():
    obj = '{"steps": "Hello world", "x": 1}'
    text, complete = _drain([obj])
    assert text == "Hello world"
    assert complete is True


def test_split_across_arbitrary_chunks():
    obj = '{"steps": "The cat sat on the mat.", "n": 2}'
    text, complete = _drain(list(obj))
    assert text == "The cat sat on the mat."
    assert complete is True


def test_escaped_quote_and_newline():
    payload = 'He said \\"hi\\"\\nThen left'
    obj = '{"steps": "' + payload + '"}'
    text, complete = _drain(list(obj))
    assert text == 'He said "hi"\nThen left'
    assert complete is True


def test_escape_split_across_chunk_boundary():
    chunks = ['{"steps": "a', '\\', 'n', 'b"}']
    text, complete = _drain(chunks)
    assert text == "a\nb"
    assert complete is True


def test_unicode_escape():
    obj = '{"steps": "caf\\u00e9 \\u2212 x"}'
    text, complete = _drain(list(obj))
    assert text == "café − x"
    assert complete is True


def test_steps_not_first_key():
    obj = '{"not_relevant": false, "steps": "ok then", "z": 3}'
    text, complete = _drain(list(obj))
    assert text == "ok then"
    assert complete is True


def test_emitted_text_matches_json_decode_for_random_content():
    value = 'Line1\nLine2 with "quotes" and \\ backslash and tab\there ✓'
    obj = json.dumps({"steps": value, "other": [1, 2, 3]})
    text, complete = _drain(list(obj))
    assert text == value
    assert complete is True


def test_missing_field_emits_nothing():
    obj = '{"other": "nope"}'
    text, complete = _drain([obj])
    assert text == ""
    assert complete is False
