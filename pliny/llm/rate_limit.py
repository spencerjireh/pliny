import asyncio
import time


class TokenBucket:
    """RPM + TPM token bucket. async-safe via internal lock.

    Refill is continuous (rate per second). `acquire` blocks until both buckets
    can satisfy the request. Conservative defaults are configured by the caller.
    """

    def __init__(self, *, rpm: int, tpm: int) -> None:
        self.rpm = rpm
        self.tpm = tpm
        self._req = float(rpm)
        self._tok = float(tpm)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._req = min(self.rpm, self._req + elapsed * (self.rpm / 60.0))
        self._tok = min(self.tpm, self._tok + elapsed * (self.tpm / 60.0))
        self._last_refill = now

    async def acquire(self, estimated_tokens: int) -> None:
        if estimated_tokens > self.tpm:
            raise ValueError(
                f"requested {estimated_tokens} tokens exceeds bucket capacity {self.tpm}"
            )
        async with self._lock:
            while True:
                self._refill()
                if self._req >= 1.0 and self._tok >= estimated_tokens:
                    self._req -= 1.0
                    self._tok -= estimated_tokens
                    return
                req_deficit = max(0.0, 1.0 - self._req) / (self.rpm / 60.0)
                tok_deficit = max(0.0, estimated_tokens - self._tok) / (self.tpm / 60.0)
                wait = max(req_deficit, tok_deficit, 0.001)
                await asyncio.sleep(wait)
