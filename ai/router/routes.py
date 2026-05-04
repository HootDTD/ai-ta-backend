from __future__ import annotations
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class RouteName(str, Enum):
    CONCEPTUAL_EXPLAINER     = "conceptual_explainer"
    STEPWISE_PROBLEM_SOLVER  = "stepwise_problem_solver"
    FACTUAL_LOOKUP           = "factual_lookup"
    DEFINITION               = "definition"
    STUDY_GUIDE_GENERATOR    = "study_guide_generator"
    CLARIFY                  = "clarify"


class RetrievalMode(str, Enum):
    NONE    = "NONE"
    AUGMENT = "AUGMENT"
    FRESH   = "FRESH"


@dataclass(frozen=True)
class Route:
    name: str
    description: str
    default_top_k: int
    rerank_enabled: bool


REGISTRY: list[Route] = [
    Route("conceptual_explainer",    "Explain how/why something works",            12, True),
    Route("stepwise_problem_solver", "Multi-step worked solution to a problem",    15, True),
    Route("factual_lookup",          "One short factual answer with citation",      8, False),
    Route("definition",              "Define a term in 25-60 words",                5, False),
    Route("study_guide_generator",   "Bullet outlines, takeaways, study material", 20, True),
    Route("clarify",                 "Fallback: ask a clarifying question",         0, False),
]

_SEEDS_PATH = Path(__file__).parent / "seeds.json"


def load_seed_utterances() -> dict[str, list[str]]:
    """Load seed utterances per route from seeds.json. Stdlib only."""
    with _SEEDS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)
