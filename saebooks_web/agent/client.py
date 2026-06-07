"""agent/client.py — OpenAI-compatible client pointed at the LiteLLM gateway.

Used exclusively by the GUI dev console (saebooks_web.routes.dev). This is the
only LLM caller in saebooks-web; everything else is a thin REST client of the
saebooks-api.

Model selection by mode (only ``dev`` is wired today):
  'dev' -> AGENT_MODEL_HEAVY (claude-opus-4-8-sub)

Env (all optional — defaults match the bosun LiteLLM gateway):
  LITELLM_BASE_URL   default http://10.0.1.1:4000/v1
  LITELLM_API_KEY    default "none"  (the live gateway REQUIRES a real key —
                     set this in the web service env or the agent loop 401s)
  AGENT_MODEL_HEAVY  default claude-opus-4-8-sub
  AGENT_MODEL_CHAT   default claude-sonnet-4-6-sub
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Mode -> model mapping
# ---------------------------------------------------------------------------

_HEAVY_MODES = {"dev"}

_DEFAULT_CHAT_MODEL = "claude-sonnet-4-6-sub"
_DEFAULT_HEAVY_MODEL = "claude-opus-4-8-sub"


def model_for_mode(mode: str) -> str:
    """Return the appropriate model alias for a given mode string."""
    mode = (mode or "dev").lower().strip()
    if mode in _HEAVY_MODES:
        return os.environ.get("AGENT_MODEL_HEAVY", _DEFAULT_HEAVY_MODEL)
    return os.environ.get("AGENT_MODEL_CHAT", _DEFAULT_CHAT_MODEL)


# ---------------------------------------------------------------------------
# Shared async client
# ---------------------------------------------------------------------------

def get_client() -> AsyncOpenAI:
    """Return an AsyncOpenAI client configured for the LiteLLM gateway.

    The client is constructed fresh each call — FastAPI/httpx handle connection
    pooling; there is no per-request penalty for this pattern.
    """
    base_url = os.environ.get("LITELLM_BASE_URL", "http://10.0.1.1:4000/v1")
    api_key = os.environ.get("LITELLM_API_KEY", "none")
    return AsyncOpenAI(base_url=base_url, api_key=api_key)
