"""OpenAI-compatible chat client (OpenAI, or any local server speaking the same API). Stdlib only."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

from ar.adapter import AdapterError

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.6-terra"


def _tls_context() -> ssl.SSLContext:
    """The OS trust store when available; python.org builds on macOS ship no CA bundle."""
    try:
        import truststore  # installed with the anthropic SDK

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_tls_installed = False


def _install_tls_opener() -> None:
    """Make urllib verify HTTPS against the OS trust store, once per process."""
    global _tls_installed
    if not _tls_installed:
        handler = urllib.request.HTTPSHandler(context=_tls_context())
        urllib.request.install_opener(urllib.request.build_opener(handler))
        _tls_installed = True


class OpenAICompatClient:
    def __init__(
        self, base_url: str = DEFAULT_BASE_URL, api_key: str = "", model: str = DEFAULT_MODEL,
        timeout: float = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._api_key = api_key

    @classmethod
    def from_env(cls, model: str | None = None, base_url: str | None = None) -> OpenAICompatClient:
        """Reads OPENAI_API_KEY and OPENAI_BASE_URL; a key is required only for the hosted URL."""
        base_url = base_url or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key and base_url.rstrip("/") == DEFAULT_BASE_URL:
            raise AdapterError("OPENAI_API_KEY is not set")
        return cls(base_url, key, model or DEFAULT_MODEL)

    def complete(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
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
                **({"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}),
            },
            method="POST",
        )
        try:
            if self.base_url.startswith("https://"):
                _install_tls_opener()
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", {}).get("message", "")
            except (ValueError, AttributeError, OSError):
                detail = ""
            raise AdapterError(f"model endpoint returned HTTP {exc.code}" + (f": {detail}" if detail else "")) from exc
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise AdapterError(f"model call failed: {type(exc).__name__}") from exc
