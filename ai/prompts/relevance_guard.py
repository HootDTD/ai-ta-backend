"""Prompt for the question-relevance guard."""


def relevance_guard_prompt(subject: str, current_topic: str | None = None) -> str:
    # A course title alone often can't cover module-level content (e.g. an
    # ethics module inside an MIS course), so callers that know what the
    # student is working on right now pass it as ``current_topic``.
    #
    # The classification rules must anchor on the SAME scope the topic line
    # establishes: with rules phrased against the subject title alone, the
    # model follows the rules over the topic line and rejects any question
    # whose module sounds like a different discipline than the course title
    # (2026-07-31 ethics-in-MIS aside bug). So when a current_topic is given,
    # scope = subject OR current module, stated inside the rules themselves.
    if current_topic:
        scope = "the course scope (its subject or the current module)"
        header = (
            f"You are a relevance guard for the {subject} course materials. "
            "The course scope is defined by BOTH the subject title AND the "
            "module the student is currently studying — course modules often "
            "cover material the subject title alone would not suggest.\n\n"
        )
        topic_block = (
            f"The student is currently studying: {current_topic}\n"
            "The course scope INCLUDES this module. A question about the "
            "current module, its problem, its terminology, or its immediate "
            'background is in scope and must be classified "full" even if the '
            f"module topic sounds like a different discipline than {subject}.\n\n"
        )
        none_rule_tail = " AND unrelated to the current module"
    else:
        scope = f"the {subject} course scope"
        header = f"You are a relevance guard for the {subject} course materials. "
        topic_block = ""
        none_rule_tail = ""
    return (
        header
        + topic_block
        + f"Determine how much of the student's question is within {scope}.\n\n"
        "Return JSON with these keys:\n"
        '  "relevance": one of "full", "partial", or "none"\n'
        '  "on_topic_portion": string — the part of the question that IS within '
        f'{scope} (empty string if relevance is "none")\n'
        '  "off_topic_portion": string — the part that is NOT within '
        f'{scope} (empty string if relevance is "full")\n'
        '  "reason": string — brief explanation of your classification\n\n'
        "Classification rules:\n"
        f'  "full": The entire question is within {scope}.\n'
        f'  "partial": Part of the question is within {scope} but part is not. '
        "Extract the on-topic portion so retrieval can target it.\n"
        f'  "none": The question has nothing to do with {scope} — purely another '
        f"discipline or general trivia{none_rule_tail}.\n\n"
        'When in doubt between "partial" and "none", choose "partial" — '
        "err on the side of answering the student."
    )
