"""Pre-warm (and locally cache) the Apollo NLI checkpoint — campaign C2.

Downloads (or loads from an existing ``HF_HOME`` cache) the active NLI
checkpoint (``APOLLO_NLI_MODEL`` env, default the tuned large model — see
``apollo.resolution.nli_config.active_nli_model``) and runs one dummy
classification, printing separate load / first-classify wall-clock
timings. These feed the campaign ops-gate latency accounting (spec
2026-07-01-system-scores-outputs-design.md §5: "no first-request HF
download").

Usage::

    HF_HOME=./.hf-cache python -m campaign.infra.prewarm_nli

Run it twice to confirm the cache contract: the FIRST run downloads the
checkpoint from Hugging Face into ``HF_HOME`` (network required); the
SECOND run against the same ``HF_HOME`` loads entirely from local disk
(no network, and should complete in well under the first run's time).
"""

from __future__ import annotations

import time

from apollo.resolution.nli_adjudicator import TransformersNLIAdjudicator
from apollo.resolution.nli_config import NLI_DEVICE, active_nli_model


def main() -> (
    None
):  # pragma: no cover - CLI shim, real-model path (requires transformers + HF download)
    model_name = active_nli_model()
    print(f"apollo_nli_prewarm: model={model_name!r} device={NLI_DEVICE!r}")

    adjudicator = TransformersNLIAdjudicator(model_name, device=NLI_DEVICE)

    t_load_start = time.monotonic()
    adjudicator._load()
    load_seconds = time.monotonic() - t_load_start
    print(f"apollo_nli_prewarm: load_seconds={load_seconds:.2f}")

    t_classify_start = time.monotonic()
    adjudicator.classify("a", "a")
    classify_seconds = time.monotonic() - t_classify_start
    print(f"apollo_nli_prewarm: first_classify_seconds={classify_seconds:.2f}")

    print(
        "apollo_nli_prewarm: done — checkpoint cached under HF_HOME; "
        "grading's boot-time prewarm() hook will now load it without a network call"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
