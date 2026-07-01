"""Apollo NLI threshold calibration harness.

Loads the labeled dev set, sweeps the 2-D threshold grid using the NLI
classifier, prints the precision-recall table, and exits non-zero if no
operating point meets the 0.95 precision floor.

This script is designed to be run **after** Task 12 installs
``transformers``/``torch``.  In CI / unit tests, pass a pre-built
``adjudicator`` to ``main()`` via the injectable keyword argument — the real
``TransformersNLIAdjudicator`` construction is ``# pragma: no cover``-tagged
so it is excluded from the patch-coverage gate.

Usage::

    .venv/Scripts/python.exe scripts/apollo_nli_calibrate.py \\
        apollo/resolution/tests/data/nli_dev_set.jsonl

Exit codes:
    0  — a best operating point with ≥ 0.95 precision was found
    1  — no grid point met the precision floor (check the dev set or thresholds)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a bare script from the repo root (mirrors other scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apollo.resolution.calibration import (  # noqa: E402
    best_operating_point,
    format_report,
    load_dev_set,
    sweep_thresholds,
)
from apollo.resolution.nli_adjudicator import NLIAdjudicator  # noqa: E402


def main(
    argv: list[str] | None = None,
    adjudicator: NLIAdjudicator | None = None,
) -> int:
    """Run the calibration sweep and return an exit code.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).
        adjudicator: Pre-built NLI adjudicator (used in tests / CI to avoid
            loading the real model).  When ``None``, the real
            ``TransformersNLIAdjudicator`` is constructed — requires the
            ``transformers`` package and a model download (Task 12).

    Returns:
        0 if a best operating point meeting the 0.95 precision floor was found;
        1 otherwise.
    """
    parser = argparse.ArgumentParser(
        description="Apollo NLI threshold calibration — sweep precision/recall grid"
    )
    parser.add_argument(
        "dev_set",
        type=Path,
        help="Path to the JSONL dev set (see apollo/resolution/tests/data/nli_dev_set.jsonl)",
    )
    args = parser.parse_args(argv)

    labeled = load_dev_set(args.dev_set)

    if adjudicator is None:  # pragma: no cover
        from apollo.resolution.nli_adjudicator import TransformersNLIAdjudicator
        from apollo.resolution.nli_config import NLI_DEVICE, NLI_MODEL_NAME

        adjudicator = TransformersNLIAdjudicator(NLI_MODEL_NAME, device=NLI_DEVICE)

    report = sweep_thresholds(labeled, adjudicator.classify)
    selected = best_operating_point(report, min_precision=0.95)
    print(format_report(report, selected))
    return 0 if selected is not None else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
