"""Claude (Anthropic API) helper used by the AI features."""
import json
import re

from fastapi import HTTPException, status

from backend.config import settings


def claude_configured() -> bool:
    return bool(settings.anthropic_api_key.strip())


def ask_claude(system: str, messages: list[dict], max_tokens: int = 1800) -> str:
    """One Messages-API call; returns the text of the answer. Raises HTTPException with a readable reason."""
    if not claude_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AI is not set up. Add ANTHROPIC_API_KEY to .env (key from console.anthropic.com) and restart the server.",
        )
    try:
        import anthropic
    except ImportError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The 'anthropic' package is missing. Run: python -m pip install anthropic"
        ) from None

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key.strip(), timeout=90.0, max_retries=2)
    try:
        response = client.messages.create(
            model=settings.ai_model, max_tokens=max_tokens, system=system, messages=messages
        )
    except anthropic.AuthenticationError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The Claude API key was rejected. Check ANTHROPIC_API_KEY in .env.") from None
    except anthropic.RateLimitError:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "The AI service is busy. Try again in a minute.") from None
    except anthropic.APIStatusError as exc:
        reason = (getattr(exc, "message", "") or "").strip()[:240]
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"The AI service returned an error ({exc.status_code}): {reason} (check AI_MODEL and your API credit at console.anthropic.com)",
        ) from None
    except anthropic.APIConnectionError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not reach the AI service. Check the server's internet connection.") from None
    return "".join(block.text for block in response.content if getattr(block, "type", "") == "text").strip()


def parse_json(text: str) -> dict:
    """Pull a JSON object out of a model answer (handles ```json fences and text around it)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON found") from None
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    return data
