"""Regression tests for the answer-blind leak guard.

The session-73 case (staging, 2026-07-14): the writer paraphrased the
reference definition of future shock into its question. The guard must reject
any wording that carries content words private to the target node — words
appearing in neither the problem text nor the student's own messages.
"""

from apollo.ontology import build_node
from apollo.smart_questions.leak_guard import leaks_private_content, private_leak_words

_SESSION_73_NODE = build_node(
    node_type="definition",
    node_id="def_future_shock",
    attempt_id=74,
    source="reference",
    content={
        "concept": "future shock",
        "meaning": (
            "Future shock is the term coined by Alvin Toffler for the "
            "psychological state of stress, disorientation, and overload that "
            "people experience when social and technological change happens too "
            "much and too fast for them to adapt to it comfortably or integrate "
            "it into their existing ways of living and thinking."
        ),
    },
)

_SESSION_73_PROBLEM = (
    "What is Future Shock, and why does it occur? When did it start happening "
    "— can you give an example? And is it still happening today — why or why not?"
)

_SESSION_73_STUDENT = [
    "future shock occurs when things are happening too quickly and it becomes difficult to keep up"
]

_SESSION_73_LEAKED_QUESTION = (
    "Why does future shock make it hard for people to actually adjust their "
    "usual ways of living and thinking, instead of just feeling like things "
    "are happening too quickly?"
)


def test_rejects_session_73_paraphrase_leak():
    assert leaks_private_content(
        _SESSION_73_LEAKED_QUESTION,
        node=_SESSION_73_NODE,
        problem_text=_SESSION_73_PROBLEM,
        student_messages=_SESSION_73_STUDENT,
    )


def test_accepts_dimension_level_question_grounded_in_problem_text():
    question = "You told me what it is — but why does it occur? I couldn't explain that part yet."
    assert not leaks_private_content(
        question,
        node=_SESSION_73_NODE,
        problem_text=_SESSION_73_PROBLEM,
        student_messages=_SESSION_73_STUDENT,
    )


def test_accepts_question_reusing_student_vocabulary():
    question = "What makes it so difficult to keep up when things happen quickly?"
    assert not leaks_private_content(
        question,
        node=_SESSION_73_NODE,
        problem_text=_SESSION_73_PROBLEM,
        student_messages=_SESSION_73_STUDENT,
    )


def test_rejects_verbatim_private_phrase():
    question = "Is it about stress and disorientation from technological change?"
    assert leaks_private_content(
        question,
        node=_SESSION_73_NODE,
        problem_text=_SESSION_73_PROBLEM,
        student_messages=_SESSION_73_STUDENT,
    )


def test_plural_variants_of_public_words_do_not_false_positive():
    # Student said "occurs"; a question using "occur" must not be treated as
    # private just because the inflection differs.
    node = build_node(
        node_type="definition",
        node_id="def_x",
        attempt_id=1,
        source="reference",
        content={"concept": "x", "meaning": "an occur event with private wording"},
    )
    question = "When does it occur?"
    assert not leaks_private_content(
        question,
        node=node,
        problem_text="Explain x.",
        student_messages=["it occurs sometimes"],
    )


def test_list_valued_content_fields_are_private_too():
    node = build_node(
        node_type="equation",
        node_id="eq1",
        attempt_id=1,
        source="reference",
        content={"symbolic": "P1 + rho*g*h = P2", "variables": ["bernoulli_head"]},
    )
    assert leaks_private_content(
        "Do we need the bernoulli_head here?",
        node=node,
        problem_text="Solve the pipe problem.",
        student_messages=["I set up the flow"],
    )


def test_private_leak_words_names_the_offending_words():
    node = build_node(
        node_type="definition",
        node_id="def_x",
        attempt_id=1,
        source="reference",
        content={"concept": "x", "meaning": "the psychological disorientation of x"},
    )
    assert private_leak_words(
        "ask about the psychological side",
        node=node,
        problem_text="Explain x.",
        student_messages=["x matters"],
    ) == {"psychological"}
    assert (
        private_leak_words(
            "ask them to explain x further",
            node=node,
            problem_text="Explain x.",
            student_messages=["x matters"],
        )
        == set()
    )
