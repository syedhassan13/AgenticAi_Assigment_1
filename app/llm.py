"""Thin LLM adapter — abstracts over Gemini / OpenAI providers.

Returns raw text + token-usage dict.  Keys come from environment via config.
Each provider is lazily imported so we only need the SDK for the one in use.
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass

from app.config import settings

log = logging.getLogger("llm")


@dataclass
class LLMResponse:
    """What every provider call returns."""
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------

async def call_llm(
    messages: list[dict],          # [{"role": "system"|"user"|"assistant", "content": "..."}]
    model: str | None = None,
    provider: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,      # low temp → more deterministic structured output
) -> LLMResponse:
    """Route to the configured provider.  Raises on missing key or unknown provider."""

    provider = provider or settings.model_provider
    model = model or settings.model_name
    max_tokens = max_tokens or settings.max_output_tokens

    if provider == "gemini":
        return await _call_gemini(messages, model, max_tokens, temperature)
    elif provider == "openai":
        return await _call_openai(messages, model, max_tokens, temperature)
    else:
        raise ValueError(f"Unknown LLM provider: {provider!r}")


# ---------------------------------------------------------------------------
# Gemini (google-genai SDK)
# ---------------------------------------------------------------------------

async def _call_gemini(
    messages: list[dict], model: str, max_tokens: int, temperature: float,
) -> LLMResponse:
    """Call Gemini via the google-genai SDK (synchronous under the hood)."""

    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set in environment")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)

    # Convert our messages to Gemini's expected format.
    # Gemini uses "user" and "model" roles, with system instruction separate.
    system_text = ""
    contents = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            # Accumulate system instructions (Gemini takes them separately)
            system_text += content + "\n"
        elif role == "user":
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text=content)],
            ))
        elif role == "assistant":
            contents.append(types.Content(
                role="model",
                parts=[types.Part(text=content)],
            ))

    config = types.GenerateContentConfig(
        system_instruction=system_text.strip() or None,
        max_output_tokens=max_tokens,
        temperature=temperature,
    )

    # Synchronous call (google-genai doesn't have a native async API yet)
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )

    # Extract token usage from response metadata
    in_tok = 0
    out_tok = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        um = response.usage_metadata
        in_tok = getattr(um, "prompt_token_count", 0) or 0
        out_tok = getattr(um, "candidates_token_count", 0) or 0

    return LLMResponse(
        text=response.text or "",
        input_tokens=in_tok,
        output_tokens=out_tok,
        model=model,
    )


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

async def _call_openai(
    messages: list[dict], model: str, max_tokens: int, temperature: float,
) -> LLMResponse:
    """Call OpenAI's chat completion API."""

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in environment")

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)

    # OpenAI uses messages directly with system/user/assistant roles
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    choice = response.choices[0]
    usage = response.usage

    return LLMResponse(
        text=choice.message.content or "",
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        model=model,
    )
