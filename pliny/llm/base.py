from typing import Any, Protocol

from pydantic import BaseModel


class ChatResponse(BaseModel):
    text: str
    usage: dict[str, int]


class LLMError(Exception):
    pass


class RateLimitedError(LLMError):
    def __init__(self, retry_after: float, message: str = "rate limited") -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CostCapExceeded(LLMError):
    pass


class LLM(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> ChatResponse: ...

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...

    async def vision(
        self,
        *,
        image_bytes: bytes,
        image_mime: str,
        prompt: str,
        model: str,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResponse: ...
