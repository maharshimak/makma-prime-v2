"""Stateless model transport; configuration is not a connection check."""
from __future__ import annotations

from typing import Any, AsyncIterator, Protocol


class ModelProvider(Protocol):
    def stream(self, *, model: str, instructions: str, messages: list,
               tools: list, reasoning: str = "") -> AsyncIterator[dict]: ...

    async def analyze_image(self, *, model: str, instructions: str,
                            data_url: str, prompt: str) -> str: ...


class OpenAIProvider:
    """Responses transport. Orchestrator owns history and the bounded tool loop."""

    def __init__(self, api_key: str, client: Any = None):
        if client is None:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=api_key, timeout=60.0, max_retries=0)
        self.client = client

    async def stream(self, *, model, instructions, messages, tools, reasoning=""):
        kwargs = dict(model=model, instructions=instructions, input=messages,
                      tools=tools, store=False, stream=True, parallel_tool_calls=False,
                      include=["reasoning.encrypted_content"])
        if reasoning:
            kwargs["reasoning"] = {"effort": reasoning}
        response_stream = await self.client.responses.create(**kwargs)
        completed = False
        try:
            async for event in response_stream:
                if event.type == "response.output_text.delta":
                    yield {"type": "delta", "text": event.delta}
                elif event.type == "response.completed":
                    completed = True
                    yield {"type": "completed", "output": [
                        item.model_dump(mode="json", exclude_none=True)
                        for item in event.response.output
                    ]}
                elif event.type in {"response.failed", "response.incomplete", "error"}:
                    raise RuntimeError("Provider did not complete the response")
            if not completed:
                raise RuntimeError("Provider stream ended before completion")
        finally:
            await response_stream.close()

    async def analyze_image(self, *, model, instructions, data_url, prompt):
        response = await self.client.responses.create(
            model=model, store=False, instructions=instructions,
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": data_url, "detail": "high"},
            ]}],
        )
        if response.status != "completed":
            raise RuntimeError("Provider did not complete image analysis")
        return response.output_text

    async def close(self):
        await self.client.close()
