"""Resolver V2's OWN lazy NLI singleton (design §5.4, task T4).

V2 must run even when ``APOLLO_NLI_ENABLED=0`` (the v1 resolver tier's kill
switch), so it does NOT reuse ``done_grading``'s process singleton — it owns
its own. The wrapped implementation IS the existing infrastructure:
:class:`apollo.resolution.nli_adjudicator.TransformersNLIAdjudicator` over
``active_nli_model()`` (cross-encoder/nli-deberta-v3-large by default, env
override ``APOLLO_NLI_MODEL``) on ``NLI_DEVICE`` (CPU). The HF cache location
(``HF_HOME`` / ``HF_HUB_OFFLINE`` → ``./.hf-cache``) is deployment-env
plumbing shared with the v1 tier — nothing here re-configures it.

Laziness contract (the CI-safety interlock): importing this module and even
calling :func:`get_adjudicator` NEVER loads the model or imports
``transformers`` — ``TransformersNLIAdjudicator`` defers both to its first
``classify`` call. When ``transformers`` is not installed at all,
:func:`get_adjudicator` degrades to ``None`` with ONE process-lived warning
(mirrors ``done_grading._log_nli_import_failure_once``); callers pass
``nli=None`` through to ``scoring.score_nodes``'s deterministic lexical-only
degrade. Tests inject :class:`FakeNLIAdjudicator` directly and never touch
this provider's real path.
"""

from __future__ import annotations

import importlib.util
import logging

from apollo.resolution.nli_adjudicator import NLIAdjudicator, TransformersNLIAdjudicator
from apollo.resolution.nli_config import NLI_DEVICE, active_nli_model

_LOG = logging.getLogger(__name__)

# Process-lived singleton + one-shot degradation log flag (module globals,
# reset by tests via monkeypatch.setattr).
_ADJUDICATOR: TransformersNLIAdjudicator | None = None
_IMPORT_FAILURE_LOGGED: bool = False


def _transformers_available() -> bool:
    """Whether the ``transformers`` package is importable — checked WITHOUT
    importing it (``find_spec`` reads metadata only), so the check itself is
    free and CI-safe."""
    try:
        return importlib.util.find_spec("transformers") is not None
    except (ImportError, ValueError):  # pragma: no cover - exotic finder states
        return False


def _log_import_failure_once() -> None:
    """Log the missing-``transformers`` degradation exactly once per process
    (not once per attempt) — V2 proceeds lexical-only."""
    global _IMPORT_FAILURE_LOGGED
    if not _IMPORT_FAILURE_LOGGED:
        _LOG.warning("resolver_v2_nli_transformers_unavailable degrading_lexical_only")
        _IMPORT_FAILURE_LOGGED = True


def get_adjudicator() -> NLIAdjudicator | None:
    """The process-lived lazy V2 NLI adjudicator, or ``None`` when
    ``transformers`` is unavailable (degrade — never raise).

    Construction is cheap and model-load-free; the deberta checkpoint loads on
    the first ``classify`` call (from the local HF cache under
    ``HF_HUB_OFFLINE=1``)."""
    global _ADJUDICATOR
    if _ADJUDICATOR is not None:
        return _ADJUDICATOR
    if not _transformers_available():
        _log_import_failure_once()
        return None
    _ADJUDICATOR = TransformersNLIAdjudicator(active_nli_model(), device=NLI_DEVICE)
    return _ADJUDICATOR
