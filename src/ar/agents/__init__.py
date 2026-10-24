"""`agents.run(role, packet)` is the only model entry point."""

from __future__ import annotations

from typing import Protocol

from ar.models import AgentResult, Role
from ar.packet import Packet


class Agent(Protocol):
    """Same packet in, same typed result out, whether fake or live."""

    def run(self, role: Role, packet: Packet) -> AgentResult: ...


_agent: Agent | None = None


def configure(agent: Agent) -> None:
    global _agent
    _agent = agent


def run(role: Role, packet: Packet) -> AgentResult:
    if _agent is None:
        raise RuntimeError("no agent configured; call agents.configure() or use --fake")
    return _agent.run(role, packet)
