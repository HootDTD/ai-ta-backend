from apollo.schemas.procedure import ProcedureStep

import pytest
from pydantic import ValidationError


def test_procedure_step_accepts_valid_fields():
    step = ProcedureStep(
        order=1,
        action="apply continuity to find v2",
        uses_equations=["continuity"],
        purpose="solve for v2 so bernoulli can be evaluated",
    )
    assert step.order == 1
    assert step.action == "apply continuity to find v2"
    assert step.uses_equations == ["continuity"]
    assert step.purpose == "solve for v2 so bernoulli can be evaluated"


def test_procedure_step_rejects_zero_order():
    with pytest.raises(ValidationError):
        ProcedureStep(order=0, action="x", uses_equations=[], purpose="y")


def test_procedure_step_rejects_empty_action():
    with pytest.raises(ValidationError):
        ProcedureStep(order=1, action="", uses_equations=[], purpose="y")


def test_procedure_step_rejects_empty_purpose():
    with pytest.raises(ValidationError):
        ProcedureStep(order=1, action="x", uses_equations=[], purpose="")


def test_procedure_step_allows_empty_uses_equations():
    step = ProcedureStep(order=1, action="state the target", uses_equations=[], purpose="frame the problem")
    assert step.uses_equations == []
