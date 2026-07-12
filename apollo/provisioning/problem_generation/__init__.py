"""Public surface for the default-OFF problem-generation authoring stage."""

from apollo.provisioning.problem_generation.generator import (
    GenerationRunResult,
    ProblemGenerationDisabled,
    generate_problem_variants,
    generation_max_variants,
    generation_token_ceiling,
    problem_generation_enabled,
)
from apollo.provisioning.problem_generation.operators import VARIATION_OPERATORS
from apollo.provisioning.problem_generation.verifiers import (
    RoundTripVerdict,
    RubricClaim,
    RubricReport,
    qualitative_rubric,
    round_trip_check,
)

__all__ = [
    "GenerationRunResult",
    "ProblemGenerationDisabled",
    "RoundTripVerdict",
    "RubricClaim",
    "RubricReport",
    "VARIATION_OPERATORS",
    "generate_problem_variants",
    "generation_max_variants",
    "generation_token_ceiling",
    "problem_generation_enabled",
    "qualitative_rubric",
    "round_trip_check",
]
