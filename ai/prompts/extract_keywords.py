"""Prompt for extracting core principles / keywords from a question."""


def extract_keywords_prompt(subject: str) -> str:
    return (
        f"You analyze {subject} textbook questions. Identify only the core principles or equations explicitly referenced in the prompt. "
        "List the topic names without elaborating or explaining them in detail. "
        "Respond with a single short sentence or comma-separated list naming the relevant topics."
    )
