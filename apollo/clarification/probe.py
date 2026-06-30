"""Answer-blind probe hints. Rule (spec §6.4): reveal the DIMENSION to pin down,
never the value. The hint is steering for the confused-classmate generator; it
must never carry the candidate's claim — the student already named the entities,
so we only ask them to COMMIT, not confirm a revealed answer."""

from __future__ import annotations

from apollo.ontology.nodes import Node
from apollo.resolution.candidates import Candidate

# Per node type: the dimension to make the student commit to. NONE of these
# strings reference the specific candidate — only the kind of thing to pin down.
_HINT_BY_TYPE: dict[str, str] = {
    "condition": "Make the student commit to the DIRECTION of the relationship "
    "they just described (which way it goes), without telling them which is correct.",
    "equation": "Ask which VARIABLE they would solve for, or how two quantities "
    "trade off, without stating the relationship yourself.",
    "simplification": "Ask under what CONDITION the step they described applies, "
    "without naming the condition.",
    "definition": "Ask the student to DEFINE the term in their own words, without "
    "giving the definition.",
    "procedure_step": "Ask the student to state the next ACTION explicitly, without "
    "performing it for them.",
    "variable_mapping": "Ask which real-world quantity their symbol stands for, "
    "without mapping it yourself.",
}

_FALLBACK = (
    "Ask the student to make their last idea more precise and commit to a specific "
    "claim, without telling them what the right answer is."
)


def build_probe_hint(node: Node, candidate: Candidate) -> str:
    """Answer-free steering string for one flagged idea. Derived purely from the
    node type; the ``candidate`` arg disambiguates which idea is being probed for
    the caller's bookkeeping but is intentionally NOT rendered into the hint."""
    return _HINT_BY_TYPE.get(node.node_type, _FALLBACK)
