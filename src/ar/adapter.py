"""Live model entry behind the Agent protocol: Claude API, one schema-repair retry."""

from __future__ import annotations

import json
from typing import Any, Protocol

import anthropic
from pydantic import ValidationError

from ar.agents.roles import get_role
from ar.models import AgentResult, ClaimDraft, ProposedTask, Role
from ar.packet import Packet

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ENVELOPE_INSTRUCTIONS = """\
Reply with a single JSON object and nothing else, shaped exactly as:
{"payload": {...}, "claims": [...], "limitations": [...], "unresolved": [...],
 "proposed_tasks": [...], "outcome": "produced" | "needs_dependency" | "unable_to_complete"}
"payload" follows the role schema above. Each item of "claims" is a claim draft
(statement, kind, epistemic_status, scope, evidence_links, derivation, limitations).
Only the evidence reader creates claims. Every other role returns "claims": [] and refers to
existing claims by id inside its payload.
Each item of "proposed_tasks" has role, objective, source_ids.
"limitations" and "unresolved" are lists of strings. Use empty lists when empty.
Do not include model_calls or any other field."""


class AdapterError(RuntimeError):
    """The model endpoint could not be reached or returned an unusable response."""


class ChatClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class AnthropicClient:
    """Claude Messages API. Credentials resolve via the SDK (ANTHROPIC_API_KEY or `ant auth login`)."""

    def __init__(
        self,
        sdk: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        max_tokens: int = 16000,
    ) -> None:
        self._sdk = sdk
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        try:
            sdk = self._sdk or anthropic.Anthropic()
            response = sdk.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APIStatusError as exc:
            raise AdapterError(f"model endpoint returned HTTP {exc.status_code}") from exc
        except anthropic.APIError as exc:
            raise AdapterError(f"model call failed: {type(exc).__name__}") from exc
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None)
            raise AdapterError(f"model refused the request (category: {category})")
        return "".join(block.text for block in response.content if block.type == "text")


def extract_json(text: str) -> dict[str, Any]:
    """First JSON object in `text`, tolerating code fences and surrounding prose."""
    decoder = json.JSONDecoder()
    first_error: json.JSONDecodeError | None = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError as exc:
            first_error = first_error or exc
            continue
        if isinstance(value, dict):
            return value
    raise first_error or json.JSONDecodeError("no JSON object found", text, 0)


def _error_text(exc: json.JSONDecodeError | ValidationError) -> str:
    if isinstance(exc, ValidationError):
        return json.dumps(exc.errors(include_url=False, include_context=False, include_input=False), default=str)
    return f"invalid JSON: {exc}"


def _repair_message(user: str, reply: str, errors: str) -> str:
    return (
        f"{user}\n\nYour previous reply was invalid.\n"
        f"Previous reply:\n{reply}\n\nValidation errors:\n{errors}\n\n"
        "Reply again with a single corrected JSON object in the required envelope."
    )


class LiveAgent:
    def __init__(self, client: ChatClient) -> None:
        self.client = client

    def _complete(self, system: str, user: str) -> str:
        try:
            return self.client.complete(system, user)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"model call failed: {type(exc).__name__}: {exc}") from exc

    def run(self, role: Role, packet: Packet) -> AgentResult:
        spec = get_role(role)
        system = (
            f"{spec.SYSTEM}\n\n{ENVELOPE_INSTRUCTIONS}\n\n"
            "The following JSON schemas are authoritative. Put the role output inside payload, "
            "not at the top level. Do not substitute an outline or a different report structure.\n"
            f"Payload JSON schema:\n{json.dumps(spec.PAYLOAD.model_json_schema())}\n"
            f"Each proposed_tasks item JSON schema:\n{json.dumps(ProposedTask.model_json_schema())}\n"
            "Use proposed_tasks: [] when no supported follow-up task is needed."
        )
        user = spec.user_message(packet)
        message, errors, calls = user, "", 0
        allowance = 2 if packet.model_call_budget is None else min(2, packet.model_call_budget)
        for _ in range(allowance):
            reply = self._complete(system, message)
            calls += 1
            try:
                return self._result(spec, role, packet, extract_json(reply), calls)
            except (json.JSONDecodeError, ValidationError) as exc:
                errors = _error_text(exc)
                message = _repair_message(user, reply, errors)
        return AgentResult(
            role=role,
            task_id=packet.task_id,
            outcome="unable_to_complete",
            unresolved=[f"model reply unavailable or invalid within the call budget: {errors}"],
            model_calls=calls,
        )

    @staticmethod
    def _result(spec: Any, role: Role, packet: Packet, envelope: dict[str, Any], calls: int) -> AgentResult:
        outcome = envelope.get("outcome", "produced")
        payload: dict[str, Any] = {}
        if outcome == "produced":
            payload = spec.PAYLOAD.model_validate(envelope.get("payload", {})).model_dump(mode="json")
        return AgentResult(
            role=role,
            task_id=packet.task_id,
            outcome=outcome,
            payload=payload,
            claims=[ClaimDraft.model_validate(c) for c in envelope.get("claims", [])] if role == "evidence_reader" else [],
            limitations=envelope.get("limitations", []),
            unresolved=envelope.get("unresolved", []),
            proposed_tasks=[ProposedTask.model_validate(t) for t in envelope.get("proposed_tasks", [])],
            model_calls=calls,
        )


PROVIDERS = ("anthropic", "openai")


def load_env(*paths) -> list[str]:
    """Set KEY=value lines from `.env` files into os.environ without overriding what is already set.

    Returns the names that were set. Values are never printed.
    """
    import os
    from pathlib import Path

    loaded: list[str] = []
    for path in map(Path, paths):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.removeprefix("export ").split("=", 1)
            name, value = name.strip(), value.strip().strip("'\"")
            if name and value and name not in os.environ:
                os.environ[name] = value
                loaded.append(name)
    return loaded


def make_client(
    provider: str = "anthropic", model: str | None = None, effort: str = "medium",
    base_url: str | None = None,
) -> ChatClient:
    """Build the live chat client for a provider. `effort` applies to Claude only."""
    if provider == "anthropic":
        return AnthropicClient(model=model or DEFAULT_MODEL, effort=effort)
    if provider == "openai":
        from ar.openai_client import OpenAICompatClient

        return OpenAICompatClient.from_env(model=model, base_url=base_url)
    raise AdapterError(f"unknown provider {provider!r}; choose one of {', '.join(PROVIDERS)}")
