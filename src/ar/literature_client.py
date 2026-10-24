"""Model adapter for literature decisions, independent of the coding worker."""

from pathlib import Path

from ar.adapter import PROVIDERS, LiveAgent, load_env, make_client


def review_agent(root: Path, *, provider: str = "anthropic", model: str | None = None,
                 base_url: str | None = None) -> LiveAgent:
    if provider not in PROVIDERS:
        raise ValueError(f"Literature provider must be one of: {', '.join(PROVIDERS)}")
    load_env(root / ".env", Path.cwd() / ".env")
    return LiveAgent(make_client(provider, model, "medium", base_url))
