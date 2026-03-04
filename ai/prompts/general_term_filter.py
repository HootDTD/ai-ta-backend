"""Prompt for filtering out generic / noisy terms."""


def general_term_filter_prompt(subject: str) -> str:
    return (
        f"You are a subject-matter expert in {subject}. Keep all subject-relevant terms unless they are obviously generic noise.\n\n"
        "Remove terms only if they are purely generic academic words (e.g., 'principle', 'concept', 'equation' by themselves) "
        "with no subject signal. KEEP multi-word phrases, named laws/equations, and domain nouns even if they look common. "
        "Err on the side of keeping terms unless they would clearly pollute retrieval.\n\n"
        "Return JSON with 'filtered_terms' containing the kept terms."
    )
