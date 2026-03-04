"""Prompt for parsing a user question into structured task fields."""


def parse_question_prompt(subject: str) -> str:
    return (
        f"You are parsing {subject} textbook problems. "
        "Extract problem_type, asked_outputs, knowns, constraints, and figure_refs. "
        "Return ONLY JSON with keys: problem_type, asked_outputs, knowns, constraints, figure_refs."
    )
