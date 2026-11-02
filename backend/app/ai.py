"""The shared AI client, built once per configuration (a FastAPI dependency)."""

from fastapi import Depends, HTTPException

from .config import Settings, get_settings
from .llm import LLMClient, LLMError, build_client

_clients: dict[tuple, LLMClient] = {}


def get_llm(settings: Settings = Depends(get_settings)) -> LLMClient:
    """Build the AI client once per configuration and reuse it across requests."""
    key = (
        settings.llm_provider,
        settings.anthropic_model,
        settings.openai_base_url,
        settings.openai_model,
        settings.max_output_tokens,
    )
    if key not in _clients:
        try:
            _clients[key] = build_client(settings)
        except LLMError as exc:
            # A missing key is a server setup problem, not the student's fault.
            raise HTTPException(status_code=503, detail=f"AI provider is not configured: {exc}")
    return _clients[key]
