"""Deterministic agent: dispatches to each role's `fake` using the baselines answer key."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ar.agents.roles import get_role
from ar.models import AgentResult, Role
from ar.packet import Packet

ANSWER_KEY_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "baselines" / "answer_key.json"


class FakeAgent:
    def __init__(self, answer_key: dict[str, Any] | None = None) -> None:
        self._key = answer_key

    @property
    def answer_key(self) -> dict[str, Any]:
        if self._key is None:
            self._key = json.loads(ANSWER_KEY_PATH.read_text(encoding="utf-8"))
        return self._key

    def run(self, role: Role, packet: Packet) -> AgentResult:
        if packet.role != role:
            raise ValueError(f"packet role {packet.role!r} does not match requested role {role!r}")
        return get_role(role).fake(packet, self.answer_key)
