"""Generate a small physics PDF for the local smoke test.

IMPORTANT (why there is a THEORY section before the problems): the Apollo
provisioning solution stage RAG-generates a reference solution grounded in the
document's retrieved chunks, and the stage-3 pairing gate then REJECTS any
solution whose claims are not *entailed by that grounding*. A problems-only PDF
gives the faithfulness judge nothing to entail against, so every generated
solution is (correctly) rejected as unfaithful. The theory section below is the
concrete BASIS for that check: it states Bernoulli's equation, the continuity
equation, the assumptions, the special cases, and one fully worked example with
explicit steps — so a faithful generated solution's claims can be grounded.

Page 1: reference/theory (the grounding basis).
Page 2: the practice problems (what the scraper extracts as candidate questions).

Uses PyMuPDF (fitz), already a backend dependency. Writes scripts/smoke_bernoulli.pdf.
"""
from __future__ import annotations

import fitz  # PyMuPDF

THEORY = """Chapter 1 - Bernoulli's Equation: Reference

1.1 Bernoulli's equation. For the steady flow of an incompressible, inviscid
fluid along a streamline, the following quantity is conserved between any two
points 1 and 2 on that streamline:

    P1 + (1/2) * rho * v1^2 + rho * g * h1  =  P2 + (1/2) * rho * v2^2 + rho * g * h2

where P is the static pressure, rho is the fluid density, v is the flow speed,
g = 9.81 m/s^2 is gravitational acceleration, and h is the elevation. The term
P is the static pressure, (1/2)*rho*v^2 is the dynamic pressure, and rho*g*h is
the gravitational (hydrostatic) term.

1.2 Continuity equation. For an incompressible fluid in steady flow, the
volumetric flow rate Q = A * v is constant, so for a pipe that changes
cross-sectional area:

    A1 * v1  =  A2 * v2

This lets you find an unknown speed from the two areas and the other speed.

1.3 Assumptions. Bernoulli's equation as written above holds only when the flow
is (a) steady, (b) incompressible (constant rho), (c) inviscid (no friction
losses), and (d) evaluated along a single streamline.

1.4 Special cases.
- Horizontal flow: when h1 = h2 the gravitational terms cancel and
  P1 + (1/2)*rho*v1^2 = P2 + (1/2)*rho*v2^2.
- Torricelli's theorem: for a large open tank with a small hole a depth h below
  the surface, the surface and the jet are both at atmospheric pressure and the
  surface speed is negligible, so Bernoulli reduces to v = sqrt(2 * g * h).
- Venturi meter: a horizontal constriction; combine continuity (A1 v1 = A2 v2)
  with horizontal Bernoulli to relate the pressure drop to the flow rate
  Q = A1 * v1.

1.5 Worked example. Water (rho = 1000 kg/m^3) flows steadily through a
horizontal pipe that narrows from area A1 = 0.10 m^2 to A2 = 0.025 m^2. In the
wide section v1 = 2.0 m/s and the gauge pressure is P1 = 150 kPa. Find v2 and P2.
Step 1 - continuity: v2 = A1 * v1 / A2 = (0.10 * 2.0) / 0.025 = 8.0 m/s.
Step 2 - horizontal Bernoulli (h1 = h2): P2 = P1 + (1/2)*rho*(v1^2 - v2^2).
Step 3 - substitute: P2 = 150000 + 0.5*1000*(2.0^2 - 8.0^2) = 150000 - 30000
       = 120000 Pa = 120 kPa.
So v2 = 8.0 m/s and P2 = 120 kPa.
"""

PROBLEMS = """Chapter 1 - Bernoulli's Equation: Problems

Problem 1. Water flows through a horizontal pipe that narrows from a
cross-sectional area of 0.08 m^2 to 0.02 m^2. The speed of the water in the
wider section is 1.5 m/s and the gauge pressure there is 120 kPa. Find the
speed and the pressure in the narrower section. Assume the water is
incompressible and the flow is steady (density = 1000 kg/m^3).

Problem 2. A large open tank is filled with water to a height of 5.0 m above a
small hole in its side. Using Bernoulli's equation, determine the speed at
which water exits the hole (Torricelli's theorem). Take g = 9.81 m/s^2.

Problem 3. An ideal fluid of density 800 kg/m^3 flows steadily up a pipe that
rises 3.0 m. At the lower point the pressure is 200 kPa and the speed is
2.0 m/s; at the upper point the cross-sectional area is half that of the lower
point. Find the gauge pressure at the upper point.

Problem 4. A Venturi meter has a throat area of 0.005 m^2 in a pipe of area
0.020 m^2 carrying water (density 1000 kg/m^3). The measured pressure drop
between the wide section and the throat is 15 kPa. Determine the volumetric
flow rate of the water through the meter.
"""


def main() -> None:
    doc = fitz.open()
    rect = fitz.Rect(50, 50, 560, 788)
    for body in (THEORY, PROBLEMS):
        page = doc.new_page()
        leftover = page.insert_textbox(rect, body, fontsize=10, fontname="helv")
        if leftover < 0:
            raise SystemExit(
                f"text overflowed the page by {leftover:.0f}pt - shorten the section"
            )
    out = "scripts/smoke_bernoulli.pdf"
    doc.save(out)
    # Verify the grounding text actually landed in the rendered PDF.
    extracted = "".join(p.get_text() for p in doc)
    for marker in ("Bernoulli's equation", "A1 * v1  =  A2 * v2", "Worked example", "Problem 4"):
        assert marker in extracted, f"missing from rendered PDF: {marker!r}"
    print(f"wrote {out}  ({len(doc)} pages, theory + problems, grounding verified)")


if __name__ == "__main__":
    main()
