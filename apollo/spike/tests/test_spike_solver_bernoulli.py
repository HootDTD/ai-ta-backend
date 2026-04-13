"""Smoke tests for the throwaway spike SymPy solver.

These tests verify the solver can produce correct answers when given
a complete, hand-written KG for Bernoulli problem 01. They do NOT
test parsing, coverage, or any production behavior — this is spike code.
"""
import math

import pytest

from apollo.spike.spike_solver import solve_problem_01


def test_solver_with_complete_kg_produces_correct_P2():
    # Problem 01: horizontal pipe, find P2.
    # Given: rho=1000, A1=0.01, P1=200_000, v1=2.0, A2=0.005
    # Continuity: v2 = A1*v1/A2 = 0.01*2/0.005 = 4.0 m/s
    # Bernoulli (horizontal): P2 = P1 + 0.5*rho*(v1**2 - v2**2)
    #                            = 200_000 + 0.5*1000*(4 - 16)
    #                            = 200_000 - 6_000 = 194_000 Pa
    kg = {
        "equations": ["rho*A1*v1 - rho*A2*v2", "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)"],
        "conditions": ["h1 == h2"],
    }
    result = solve_problem_01(kg)
    assert result["success"] is True
    assert math.isclose(float(result["value"]), 194000.0, rel_tol=1e-6)


def test_solver_with_missing_continuity_fails():
    kg = {
        "equations": ["P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)"],
        "conditions": ["h1 == h2"],
    }
    result = solve_problem_01(kg)
    assert result["success"] is False
    assert "v2" in result["missing"]


def test_solver_with_missing_bernoulli_fails():
    kg = {
        "equations": ["rho*A1*v1 - rho*A2*v2"],
        "conditions": ["h1 == h2"],
    }
    result = solve_problem_01(kg)
    assert result["success"] is False
