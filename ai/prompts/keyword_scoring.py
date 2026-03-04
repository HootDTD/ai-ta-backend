"""Prompt for scoring / ranking candidate lookup terms."""


def keyword_scoring_prompt(subject: str) -> str:
    return (
        f"You rank lookup terms for the subject \"{subject}\". "
        "Use the student's question, the concise context summary, and the list of candidate terms. "
        "Assign each term a UNIQUE numeric score between 0.00 and 1.00 (two decimals, as numbers). "
        "1.00 represents the most relevant term. "
        "Return JSON {\"ranked\": [{\"term\": \"...\", \"score\": 0.95}, ...]} sorted from highest to lowest score."
    )
