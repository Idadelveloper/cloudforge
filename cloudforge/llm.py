"""Claude API wrapper for CloudForge.

Uses the current Anthropic SDK surface:
- claude-opus-5 by default (override with CLOUDFORGE_MODEL); the July 2026
  pilots ran claude-opus-4-8, which became a legacy model during the project
  window, so the final campaign is frozen on the current Opus instead
- adaptive thinking (`thinking={"type": "adaptive"}`)
- streaming with `get_final_message()` so long generations don't hit HTTP
  timeouts
- structured outputs via `output_config.format` so file sets come back as
  guaranteed-valid JSON instead of fenced markdown that needs scraping
- prompt caching on the stable system prompt (spec/plan/errors stay in the
  user turn so the cached prefix survives across calls)
"""

import json
import os
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = os.environ.get("CLOUDFORGE_MODEL", "claude-opus-5")
MAX_TOKENS = int(os.environ.get("CLOUDFORGE_MAX_TOKENS", "32000"))

# Pricing per million tokens for cost metrics. Verified 2026-08-30 against
# the official model pages: claude-opus-5 and claude-opus-4-8 share the same
# prices, so pilot and final-campaign cost estimates stay comparable.
PRICE_PER_MTOK = {
    "input": 5.00,
    "output": 25.00,
    "cache_read": 0.50,
    "cache_write": 6.25,
}

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


class GenerationError(RuntimeError):
    """Raised when the model can't produce a usable result."""


def generate_json(
    system: str, user: str, schema: dict, purpose: str
) -> tuple[Any, dict]:
    """One structured-output generation call. Returns (parsed_json, usage)."""
    try:
        with get_client().messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise GenerationError(
            "Anthropic authentication failed. Set ANTHROPIC_API_KEY in your "
            "environment or .env file."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise GenerationError(f"Network error calling the Claude API: {exc}") from exc
    except anthropic.APIStatusError as exc:
        raise GenerationError(
            f"Claude API error ({exc.status_code}): {exc.message}"
        ) from exc

    if message.stop_reason == "refusal":
        raise GenerationError(
            f"The model declined the {purpose} request (stop_reason=refusal)."
        )
    if message.stop_reason == "max_tokens":
        raise GenerationError(
            f"The {purpose} generation hit the {MAX_TOKENS}-token output limit; "
            "raise CLOUDFORGE_MAX_TOKENS or simplify the specification."
        )

    text = next(
        (block.text for block in message.content if block.type == "text"), None
    )
    if text is None:
        raise GenerationError(f"The {purpose} response contained no text block.")

    usage = {
        "purpose": purpose,
        "model": message.model,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "cache_read_input_tokens": message.usage.cache_read_input_tokens or 0,
        "cache_creation_input_tokens": message.usage.cache_creation_input_tokens or 0,
    }
    return json.loads(text), usage


def estimate_cost_usd(usage_records: list[dict]) -> float:
    """Estimate spend across all calls in a run (per-spec cost metric)."""
    total = 0.0
    for u in usage_records:
        total += u.get("input_tokens", 0) / 1e6 * PRICE_PER_MTOK["input"]
        total += u.get("output_tokens", 0) / 1e6 * PRICE_PER_MTOK["output"]
        total += (
            u.get("cache_read_input_tokens", 0) / 1e6 * PRICE_PER_MTOK["cache_read"]
        )
        total += (
            u.get("cache_creation_input_tokens", 0)
            / 1e6
            * PRICE_PER_MTOK["cache_write"]
        )
    return round(total, 4)
