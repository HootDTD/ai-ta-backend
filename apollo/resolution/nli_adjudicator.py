from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class NLIResult:
    label: str  # "entailment" | "neutral" | "contradiction"
    entailment: float
    contradiction: float
    neutral: float
    model_name: str


class NLIAdjudicator(Protocol):
    def classify(self, premise: str, hypothesis: str) -> NLIResult: ...


def normalize_nli_output(raw: Any, model_name: str) -> NLIResult:
    """Map a transformers text-classification (top_k=None) output to NLIResult.

    Maps by LABEL NAME (case-insensitive), never by index — deberta-v3 and
    roberta-mnli use different index orders.
    """
    rows = raw[0] if raw and isinstance(raw[0], list) else raw
    scores = {str(d["label"]).lower(): float(d["score"]) for d in rows}
    ent = scores.get("entailment", 0.0)
    con = scores.get("contradiction", 0.0)
    neu = scores.get("neutral", 0.0)
    label = max(
        ("entailment", ent),
        ("neutral", neu),
        ("contradiction", con),
        key=lambda kv: kv[1],
    )[0]
    return NLIResult(label, ent, con, neu, model_name)


class FakeNLIAdjudicator:
    def __init__(self, scripted: dict[tuple[str, str], NLIResult]):
        self._scripted = scripted

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        return self._scripted[(premise, hypothesis)]


class TransformersNLIAdjudicator:
    def __init__(self, model_name: str, device: str | int | None = None):
        self.model_name = model_name
        self.device = device
        self._pipe = None

    def _load(
        self,
    ):  # pragma: no cover - requires a model download; covered by the live probe (Task 12)
        if self._pipe is None:
            from transformers import pipeline

            self._pipe = pipeline(
                "text-classification",
                model=self.model_name,
                device=self.device,
                top_k=None,
            )
        return self._pipe

    def classify(
        self, premise: str, hypothesis: str
    ) -> NLIResult:  # pragma: no cover - real-model path
        pipe = self._load()
        raw = pipe({"text": premise, "text_pair": hypothesis}, truncation=True)
        return normalize_nli_output(raw, self.model_name)


def prewarm() -> None:
    """Force the active NLI checkpoint to load (and download into ``HF_HOME``
    if not already cached) NOW, plus run one dummy classification, so the
    first live grading request is served from a warm model instead of
    triggering a lazy first-load / Hugging Face download on the request path.

    Constructs its own :class:`TransformersNLIAdjudicator` — it does NOT
    populate ``apollo.handlers.done_grading``'s process-lived singleton.
    Once the checkpoint files are cached under ``HF_HOME``, the grading
    path's own lazy singleton construction reads from local disk (no
    network), which is the guarantee this function exists to provide.
    """
    from apollo.resolution.nli_config import NLI_DEVICE, active_nli_model

    adjudicator = TransformersNLIAdjudicator(active_nli_model(), device=NLI_DEVICE)
    adjudicator.classify("a", "a")
