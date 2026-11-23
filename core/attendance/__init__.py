"""Attendance + impact-radius estimation for canonical events.

Ported from notebooks/interacting_with_groq.ipynb, version "v3 + priors"
(sections 18-21): the LLM answers with a category prior and its estimate,
code clamps the number into the prior's range, applies verified capacities
as ceilings, rounds, and derives the radius from attendance.

Entry point: core.attendance.estimator.estimate_events().
"""
