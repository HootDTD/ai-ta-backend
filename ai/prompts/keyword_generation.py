"""Prompt for generating candidate lookup terms from a question."""

KEYWORD_GENERATION_PROMPT = (
    "Generate candidate lookup terms using ONLY the student's raw question and the context summary. "
    "Do not add outside knowledge. "
    "Each candidate must be a single lowercase word (no spaces, hyphens, or punctuation). "
    "If a necessary term needs multiple words to make sense, separate these two words with underscores. (only do this if the single word alternative could mean something else). "
    "Focus on including discrete concepts, principles, or keywords that directly relate to the question and context."
    "ALWAYS include individual terms from every topic mentioned in the context summary (e.g., if context contains 'Bernoulli's Principle', include 'bernoulli', 'principle')."
    "Avoid general terms, or overly broad concepts."
    "Return JSON {\"terms\": [\"term1\", ...]} with at most 20 short entries, each representing a discrete concept."
    "Be GENEROUS in proposing terms, as long as they are relevant to the question and context. Try to reach 20 terms if possible."
)


def keyword_generation_prompt() -> str:
    return KEYWORD_GENERATION_PROMPT
