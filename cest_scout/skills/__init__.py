"""Skill modules for the CEST_Scout agents.

A skill is a self-contained capability an agent can call as an OpenAI
function-calling tool. Each module exposes:
  TOOL_SPEC: an OpenAI chat-completions function-calling tool spec.
  call(args: dict) -> dict: the handler invoked with the model's parsed arguments.

Each agent wires in only the skills relevant to its role (see the
`skills=[...]` argument at each `agents/*_agent.py` call site), not every
skill in this package.
"""
from cest_scout.skills import bloch_bridge, frequency_calc, meta_analysis, pubmed, pubmed_clinical

__all__ = ["bloch_bridge", "frequency_calc", "meta_analysis", "pubmed", "pubmed_clinical"]
