"""Incremental extraction of a single top-level JSON string field from a streaming buffer.

Used to stream the solver's ``steps`` prose out of a JSON-mode LLM response before the
full object has arrived. Only handles a top-level string field; nested objects are not
searched. Decodes standard JSON escapes and never emits a partial escape sequence that
straddles a chunk boundary.
"""

from __future__ import annotations

import json


class JsonStringFieldStreamer:
    """Feed raw JSON text chunks; get back decoded deltas of one string field.

    Usage:
        s = JsonStringFieldStreamer(field="steps")
        delta = s.feed(chunk)   # returns newly-decoded text (may be "")
        ...
        s.complete              # True once the field's closing quote was seen
    """

    def __init__(self, field: str) -> None:
        self._needle = f'"{field}"'
        self._buf = ""  # unconsumed raw tail (inside-value chars not yet safe to decode)
        self._in_value = False  # have we entered the string value?
        self._started = False  # have we located the field and its opening quote?
        self.complete = False

    def feed(self, chunk: str) -> str:
        if self.complete or not chunk:
            return ""
        self._buf += chunk

        if not self._started:
            idx = self._buf.find(self._needle)
            if idx == -1:
                keep = len(self._needle) - 1
                if len(self._buf) > keep:
                    self._buf = self._buf[-keep:]
                return ""
            after = idx + len(self._needle)
            rest = self._buf[after:]
            colon = rest.find(":")
            if colon == -1:
                return ""
            tail = rest[colon + 1 :].lstrip()
            if not tail:
                return ""
            if tail[0] != '"':
                self.complete = True
                return ""
            self._started = True
            self._in_value = True
            self._buf = tail[1:]

        return self._consume_value()

    def _consume_value(self) -> str:
        out = []
        i = 0
        b = self._buf
        n = len(b)
        while i < n:
            ch = b[i]
            if ch == "\\":
                if i + 1 >= n:
                    break
                esc = b[i + 1]
                if esc == "u":
                    if i + 6 > n:
                        break
                    seq = b[i : i + 6]
                    out.append(json.loads('"' + seq + '"'))
                    i += 6
                    continue
                out.append(json.loads('"\\' + esc + '"'))
                i += 2
                continue
            if ch == '"':
                self.complete = True
                self._in_value = False
                self._buf = ""
                return "".join(out)
            out.append(ch)
            i += 1
        self._buf = b[i:]
        return "".join(out)
