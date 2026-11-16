from __future__ import annotations

import json
import os
from typing import Any

import httpx


class AsyncLLMClient:
    """Async client for Groq's OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("STAGE3_LLM_BASE_URL")
            or os.getenv("GROQ_BASE_URL")
            or "https://api.groq.com/openai/v1"
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.getenv("GROQ_API_KEY")
            or os.getenv("STAGE3_LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY", "")
        )
        self.model = (
            model
            or os.getenv("STAGE3_LLM_MODEL")
            or "openai/gpt-oss-120b"
        )
        self.timeout = timeout

    async def complete_json(self, system_prompt: str, user_payload: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("Set GROQ_API_KEY to enable Groq-powered Stage 3 review.")
        url = self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("LLM endpoint returned an unexpected response shape.") from exc
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("LLM response was not valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("LLM JSON response must be an object.")
        return parsed
