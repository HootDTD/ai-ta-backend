from __future__ import annotations

"""Facade for executing local Python code with optional unit support.

Only ``np``, ``sp`` (if available), and ``ureg`` (if available) are exposed to the
executed code. No filesystem, network, or arbitrary imports are available; using
forbidden builtins like ``open`` or ``__import__`` raises ``RuntimeError('forbidden builtin')``.
"""

import contextlib
import hashlib
import io
from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np
try:  # Optional deps
    import sympy as sp  # type: ignore
except Exception:  # pragma: no cover - optional
    sp = None
try:  # Optional deps
    from pint import UnitRegistry  # type: ignore
except Exception:  # pragma: no cover - optional
    UnitRegistry = None

ureg = UnitRegistry() if UnitRegistry else None
DEFAULT_UNITS: Dict[str, Any] = {}
if ureg:
    DEFAULT_UNITS = {
        "rho": ureg.kilogram / (ureg.meter ** 3),
        "mu": ureg.kilogram / (ureg.meter * ureg.second),
        "nu": ureg.meter ** 2 / ureg.second,
        "U": ureg.meter / ureg.second,
        "L": ureg.meter,
    }


@dataclass
class CodeResult:
    stdout: str
    globals: Dict[str, Any]
    code_hash: str
    vars_created: List[str]


def run_python(code: str, env: Dict[str, Any] | None = None) -> CodeResult:
    """Execute Python code in a restricted environment and capture stdout."""

    env = env or {}
    def _forbidden(*_args, **_kwargs):  # pragma: no cover - safety
        raise RuntimeError("forbidden builtin")

    safe_builtins = {
        "print": print,
        "range": range,
        "len": len,
        "abs": abs,
        "min": min,
        "max": max,
        "float": float,
        "int": int,
        "__import__": _forbidden,
        "open": _forbidden,
        "input": _forbidden,
        "eval": _forbidden,
        "exec": _forbidden,
    }
    local_env: Dict[str, Any] = {"__builtins__": safe_builtins, "np": np}
    if sp is not None:
        local_env["sp"] = sp
    if ureg is not None:
        local_env["ureg"] = ureg
        local_env.update(DEFAULT_UNITS)
    local_env.update(env)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, local_env)
    out = buf.getvalue()
    preview = out[:1000]
    reserved = {"np", "sp", "ureg"} | set(DEFAULT_UNITS.keys())
    keys = [k for k in local_env.keys() if not k.startswith("__") and k not in reserved]
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    globals_out = {k: v for k, v in local_env.items() if k in keys}
    return CodeResult(stdout=preview, globals=globals_out, code_hash=digest, vars_created=keys)


__all__ = ["run_python", "CodeResult", "ureg"]
