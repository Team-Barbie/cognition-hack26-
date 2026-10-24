"""Live model entry behind the Agent protocol: OpenAI-compatible chat, one repair retry."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

from pydantic import ValidationError

from ar.agents.roles import get_role
from ar.models import AgentResult, ClaimDraft, ProposedTask, Role
from ar.packet import Packet

DEFAULT_BASE_URL = "https://api.openai.com/v1"

ENVELOPE_INSTRUCTIONS = """\
Reply with a single JSON object and nothing else, shaped exactly as:
{"payload": {...}, "claims": [...], "limitations": [...], "unresolved": [...],
 "proposed_tasks": [...], "outcome": "produced" | "needs_dependency" | "unable_to_complete"}
"payload" follows the role schema above. Each item of "claims" is a claim draft
(statement, kind, epistemic_status, scope, evidence_links, derivation, limitations).
Each item of "proposed_tasks" has role, objective, source_ids.
"limitations" and "unresolved" are lists of strings. Use empty lists when empty.
Do not include model_calls or any other field."""


class AdapterError(RuntimeError):
    """The model endpoint could not be reached or returned an unusable response."""


class ChatClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._api_key = api_key

    @classmethod
    def from_env(cls) -> OpenAICompatClient:
        key = os.environ.get("AR_API_KEY")
        if not key:
            raise AdapterError("AR_API_KEY is not set")
        model = os.environ.get("AR_MODEL")
        if not model:
            raise AdapterError("AR_MODEL is not set")
        return cls(os.environ.get("AR_BASE_URL", DEFAULT_BASE_URL), key, model)

    def complete(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            raise AdapterError(f"model endpoint returned HTTP {exc.code}") from exc
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise AdapterError(f"model call failed: {type(exc).__name__}") from exc


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
        return json.dumps(exc.errors(include_url=False, include_context=False), default=str)
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
        system = f"{spec.SYSTEM}\n\n{ENVELOPE_INSTRUCTIONS}"
        user = spec.user_message(packet)
        message, errors, calls = user, "", 0
        for _ in range(2):
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
            unresolved=[f"model reply failed validation after one repair: {errors}"],
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
            claims=[ClaimDraft.model_validate(c) for c in envelope.get("claims", [])],
            limitations=envelope.get("limitations", []),
            unresolved=envelope.get("unresolved", []),
            proposed_tasks=[ProposedTask.model_validate(t) for t in envelope.get("proposed_tasks", [])],
            model_calls=calls,
        )
