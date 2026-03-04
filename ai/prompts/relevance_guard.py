"""Prompt for the question-relevance guard."""


def relevance_guard_prompt(subject: str) -> str:
    return (
        f"You are a guard for the {subject} course materials. "
        "Decide if the student's question requires knowledge from this subject. "
        "Return JSON with keys 'relevant' (bool) and 'reason' (string). "
        "Mark relevant=false if the question is primarily about another discipline or general trivia."
    )
