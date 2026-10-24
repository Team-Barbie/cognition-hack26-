"""`policies/permissions.yaml`: role capabilities, project denials, and the effective intersection."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError, field_validator

from ar.models import Record
from ar.snapshots import BrokerError

__all__ = ["PermissionPolicy", "default_policy", "effective_capabilities", "load_policy"]


class PermissionPolicy(Record):
    version: int = 1
    roles: dict[str, list[str]]
    denied: list[str] = []

    @field_validator("denied")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        return [_capability(v) for v in value]

    @field_validator("roles")
    @classmethod
    def _role_names(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        return {role: [_capability(c) for c in caps] for role, caps in value.items()}


def _capability(name: str) -> str:
    domain, dot, action = name.partition(".")
    if not (dot and domain.isidentifier() and action.isidentifier()):
        raise ValueError(f"capability {name!r} must look like 'files.read'")
    return name


def default_policy() -> PermissionPolicy:
    return PermissionPolicy(
        roles={
            "planner": ["files.read", "tasks.submit"],
            "evidence_reader": ["files.read", "tasks.submit"],
            "writer": ["files.read", "artifacts.create", "artifacts.revise", "tasks.submit"],
            "reviewer": ["files.read", "tasks.submit"],
            "source_scout": ["files.read", "tasks.submit"],
            "theorist": ["files.read", "artifacts.create", "tasks.submit"],
            "experiment_runner": ["files.read", "experiments.execute", "tasks.submit"],
            "reproducer": ["files.read", "experiments.execute", "tasks.submit"],
            "analyst": ["files.read", "artifacts.create", "tasks.submit"],
            "research_agent": ["files.read", "artifacts.create", "tasks.submit"],
            "adversarial_effect_agent": ["files.read", "tasks.submit"],
        }
    )


def load_policy(path: Path) -> PermissionPolicy:
    """Parse and validate a permissions file; an unreadable or invalid file is ACCESS_DENIED."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return PermissionPolicy.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise BrokerError("ACCESS_DENIED", f"invalid permissions file {path}: {exc}") from None


def effective_capabilities(
    role: str,
    task_allowed: Iterable[str],
    policy: PermissionPolicy,
    host_restrictions: Iterable[str] | None = None,
) -> frozenset[str]:
    """Role grants, task grants, project policy, and host restrictions intersected.

    `host_restrictions=None` means the host imposes none. Unknown roles get nothing. Nothing a
    paper or plan says can widen the result; skills add methods, not capabilities.
    """
    caps = frozenset(policy.roles.get(role, ())) & frozenset(task_allowed)
    caps -= frozenset(policy.denied)
    if host_restrictions is not None:
        caps &= frozenset(host_restrictions)
    return caps
