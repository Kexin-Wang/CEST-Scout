"""Environment/config loading for CEST_Scout.

Reads secrets and model choices from a .env file in the project root (never
committed; see .gitignore) rather than requiring them to be exported by hand
every session.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from math import pi as _pi
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

FIELD_STRENGTHS_T = (0.5, 1.5, 3.0, 5.0, 7.0)

# Below this with-vs-without-metabolite Bloch-McConnell difference, we don't call a field
# strength "recommended" -- literature MTRasym numbers are not trusted at all (see
# physicist_agent's SYSTEM_PROMPT); ~0.5% is the practical floor for this effect being
# distinguishable from simulation/parameter noise. Shared by pipeline.py (per-field-strength
# recommendation) and experimentalist_agent.py (which candidate metabolites are worth an in vivo
# verification scan at all) -- kept here rather than in pipeline.py to avoid a circular import
# (pipeline.py imports the agents, so an agent can't import the threshold back from pipeline.py).
CONTRAST_THRESHOLD_PERCENT = 0.5

# Proton gyromagnetic ratio, MHz/T. Kept identical to CEST_simulator's
# bloch_mcconnell.GYRO_1H_MHZ_PER_T so frequency-offset numbers agree
# between the chemist agent's calculator and the physicist agent's
# Bloch-McConnell simulation.
GYRO_1H_MHZ_PER_T = 267.5154109126 / (2 * _pi)

# The three open-weight models CEST_Scout was evaluated with, by the names hpc/presets serve them
# under. LLM_PRESET=<one of these> sets all four agent model names and switches thinking off
# (vLLM's Qwen3 and DeepSeek-V4 templates both honour enable_thinking=False); explicit
# OPENAI_MODEL_* / LLM_DISABLE_THINKING still win. LLM_PRESET=openai (the default) keeps gpt-5.5.
LOCAL_MODEL_PRESETS = ("deepseek-v4-flash", "qwen3.8-27b", "qwen3.6-35b-a3b")


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    openai_base_url: str | None
    model_literature: str
    model_chemist: str
    model_physicist: str
    model_experimentalist: str
    ncbi_email: str
    ncbi_api_key: str | None
    disable_thinking: bool
    # Optional override so the physicist agent (whose Bloch-McConnell tool-calling loop needs far
    # more context than a local model's window allows -- 5 field strengths x every candidate
    # metabolite) can run against real OpenAI while literature/chemist stay on a local endpoint.
    # Unset means the physicist agent uses the same client as the other two agents.
    physicist_api_key: str | None
    physicist_base_url: str | None
    # Whether the physicist override endpoint also needs Qwen3's thinking mode disabled -- true
    # when it's another local Qwen3 instance, false (the default) when it's real OpenAI.
    physicist_disable_thinking: bool
    # Name of the chat-template variable that switches thinking off: Qwen3 uses
    # "enable_thinking", DeepSeek V3.1+ / V4 use "thinking". Set LLM_THINKING_KWARG per preset.
    thinking_kwarg: str = "enable_thinking"
    # Whether the clinical-evidence stage may read PubMed Central open-access full texts to fill
    # group statistics an abstract omits (CLINICAL_FULLTEXT=false switches it off).
    clinical_fulltext: bool = True


def load_settings() -> Settings:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Put it in "
            f"{PROJECT_ROOT / '.env'} (see .env.example)."
        )
    ncbi_email = os.environ.get("NCBI_EMAIL", "").strip()
    if not ncbi_email:
        raise RuntimeError(
            "NCBI_EMAIL is not set. NCBI requires a contact email for "
            f"E-utilities requests; put it in {PROJECT_ROOT / '.env'}."
        )
    preset = os.environ.get("LLM_PRESET", "").strip().lower() or "openai"
    if preset != "openai" and preset not in LOCAL_MODEL_PRESETS:
        raise RuntimeError(f"LLM_PRESET={preset!r} is not one of: openai, {', '.join(LOCAL_MODEL_PRESETS)}.")
    if preset != "openai" and not os.environ.get("OPENAI_BASE_URL"):
        raise RuntimeError(f"LLM_PRESET={preset} needs OPENAI_BASE_URL, the vLLM server serving it "
                           "(e.g. http://localhost:8000/v1).")
    default_model = "gpt-5.5" if preset == "openai" else preset
    return Settings(
        openai_api_key=api_key,
        openai_base_url=os.environ.get("OPENAI_BASE_URL") or None,
        model_literature=os.environ.get("OPENAI_MODEL_LITERATURE", default_model),
        model_chemist=os.environ.get("OPENAI_MODEL_CHEMIST", default_model),
        model_physicist=os.environ.get("OPENAI_MODEL_PHYSICIST", default_model),
        model_experimentalist=os.environ.get("OPENAI_MODEL_EXPERIMENTALIST", default_model),
        ncbi_email=ncbi_email,
        ncbi_api_key=os.environ.get("NCBI_API_KEY") or None,
        # Qwen3's thinking mode interleaves <think>...</think> reasoning into the same
        # content field used for tool-call XML and the final JSON extraction, which can
        # break both. On by default for the local presets; leave unset for OpenAI.
        disable_thinking=os.environ.get("LLM_DISABLE_THINKING", "" if preset == "openai" else "true").strip().lower()
        in ("1", "true", "yes"),
        physicist_api_key=os.environ.get("PHYSICIST_OPENAI_API_KEY") or None,
        # The openai SDK falls back to the OPENAI_BASE_URL env var when base_url=None is passed
        # explicitly, which here points at the local vLLM server -- so an unset override must
        # resolve to the real OpenAI endpoint explicitly, not None.
        physicist_base_url=os.environ.get("PHYSICIST_OPENAI_BASE_URL") or "https://api.openai.com/v1",
        physicist_disable_thinking=os.environ.get("PHYSICIST_LLM_DISABLE_THINKING", "").strip().lower() in ("1", "true", "yes"),
        thinking_kwarg=os.environ.get("LLM_THINKING_KWARG", "enable_thinking").strip() or "enable_thinking",
        clinical_fulltext=os.environ.get("CLINICAL_FULLTEXT", "true").strip().lower() not in ("0", "false", "no"),
    )
