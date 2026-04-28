import base64
from decimal import Decimal
from typing import Any

from openai import APIStatusError, AsyncOpenAI, RateLimitError
from sqlalchemy.ext.asyncio import async_sessionmaker

from pliny.llm.base import ChatResponse, LLMError, RateLimitedError
from pliny.llm.cost_cap import check_cap, estimate_cost_usd, record_spend
from pliny.llm.rate_limit import TokenBucket


class OpenAILLM:
    def __init__(
        self,
        *,
        api_key: str,
        rpm: int,
        tpm: int,
        daily_cap_usd: Decimal,
        sm: async_sessionmaker[Any],
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._bucket = TokenBucket(rpm=rpm, tpm=tpm)
        self._cap = daily_cap_usd
        self._sm = sm

    async def _gate(self, estimated_tokens: int) -> None:
        async with self._sm() as session:
            await check_cap(session, self._cap)
        await self._bucket.acquire(estimated_tokens)

    async def _record(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        usd = estimate_cost_usd(
            model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
        async with self._sm() as session:
            await record_spend(session, usd)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> ChatResponse:
        text_chars = sum(
            len(m.get("content", "")) if isinstance(m.get("content"), str) else 0 for m in messages
        )
        estimate = max(50, text_chars // 4)
        await self._gate(estimate)
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format
            resp = await self._client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            retry_after = float(e.response.headers.get("retry-after", "1"))
            raise RateLimitedError(retry_after=retry_after, message=str(e)) from e
        except APIStatusError as e:
            raise LLMError(f"openai status {e.status_code}: {e}") from e

        usage = resp.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        await self._record(model, prompt_tokens, completion_tokens)

        text = resp.choices[0].message.content or ""
        return ChatResponse(
            text=text,
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        )

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        estimate = max(50, sum(len(t) for t in texts) // 4)
        await self._gate(estimate)
        try:
            resp = await self._client.embeddings.create(model=model, input=texts)
        except RateLimitError as e:
            retry_after = float(e.response.headers.get("retry-after", "1"))
            raise RateLimitedError(retry_after=retry_after, message=str(e)) from e
        except APIStatusError as e:
            raise LLMError(f"openai status {e.status_code}: {e}") from e
        await self._record(model, resp.usage.prompt_tokens if resp.usage else 0, 0)
        return [d.embedding for d in resp.data]

    async def vision(
        self,
        *,
        image_bytes: bytes,
        image_mime: str,
        prompt: str,
        model: str,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResponse:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image_mime};base64,{b64}"},
                    },
                ],
            }
        ]
        estimate = max(200, len(prompt) // 4 + 1000)
        await self._gate(estimate)
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": 0.0,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format
            resp = await self._client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            retry_after = float(e.response.headers.get("retry-after", "1"))
            raise RateLimitedError(retry_after=retry_after, message=str(e)) from e
        except APIStatusError as e:
            raise LLMError(f"openai status {e.status_code}: {e}") from e

        usage = resp.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        await self._record(model, prompt_tokens, completion_tokens)

        text = resp.choices[0].message.content or ""
        return ChatResponse(
            text=text,
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        )
