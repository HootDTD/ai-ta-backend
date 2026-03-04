"""Prompt templates for the AI-TA pipeline.

Each module exports either a constant string or a function that returns a
prompt string (when the prompt requires dynamic values like ``subject``).
"""

from .relevance_guard import relevance_guard_prompt
from .score_and_answer_snippet import score_and_answer_snippet_prompt
from .extract_keywords import extract_keywords_prompt
from .keyword_generation import keyword_generation_prompt
from .keyword_scoring import keyword_scoring_prompt
from .general_term_filter import general_term_filter_prompt
from .synonyms import synonyms_prompt
from .concept_extraction import concept_extraction_prompt
from .parse_question import parse_question_prompt
from .tutor import tutor_prompt

__all__ = [
    "relevance_guard_prompt",
    "score_and_answer_snippet_prompt",
    "extract_keywords_prompt",
    "keyword_generation_prompt",
    "keyword_scoring_prompt",
    "general_term_filter_prompt",
    "synonyms_prompt",
    "concept_extraction_prompt",
    "parse_question_prompt",
    "tutor_prompt",
]
