"""Prompt for proposing synonym / alternate lookup terms."""


def synonyms_prompt(subject: str) -> str:
    return (
        "You help with textbook lookup. For each concept term, propose up to two "
        f"alternate keywords, abbreviations, or symbols that might appear in {subject} materials. "
        "Return a JSON object mapping each input term to an array of 0-2 strings."
    )
