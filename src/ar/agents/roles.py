"""Role registry. Each role module exposes PAYLOAD, SYSTEM, user_message and fake."""

from __future__ import annotations

import importlib
from types import ModuleType

ROLE_MODULES = {
    "planner": "ar.agents.planner",
    "evidence_reader": "ar.agents.reader",
    "writer": "ar.agents.writer",
    "reviewer": "ar.agents.reviewer",
    "source_scout": "ar.agents.scout",
    "theorist": "ar.agents.theorist",
    "analyst": "ar.agents.analyst",
    "research_agent": "ar.agents.research_agent",
    "adversarial_reviewer": "ar.agents.adversarial_reviewer",
}

OVERRIDES: dict[str, ModuleType] = {}


def get_role(role: str) -> ModuleType:
    if role in OVERRIDES:
        return OVERRIDES[role]
    if role not in ROLE_MODULES:
        raise KeyError(f"unknown role: {role}")
    return importlib.import_module(ROLE_MODULES[role])
