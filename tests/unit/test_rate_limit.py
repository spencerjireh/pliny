import asyncio
import time

from pliny.llm.rate_limit import TokenBucket


async def test_acquire_immediate_when_under_capacity() -> None:
    b = TokenBucket(rpm=600, tpm=600_000)  # 10 rps, 10000 tps
    start = time.monotonic()
    await b.acquire(100)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05


async def test_acquire_waits_when_over_rpm() -> None:
    b = TokenBucket(rpm=120, tpm=10_000_000)  # 2 req/s, ample tokens
    # spend the bucket
    for _ in range(120):
        await b.acquire(1)
    start = time.monotonic()
    await b.acquire(1)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4


async def test_acquire_waits_when_over_tpm() -> None:
    b = TokenBucket(rpm=10_000, tpm=600)  # 10 tokens/sec
    await b.acquire(600)
    start = time.monotonic()
    await b.acquire(60)
    elapsed = time.monotonic() - start
    assert elapsed >= 5.0


async def test_concurrent_acquires_serialize() -> None:
    b = TokenBucket(rpm=60, tpm=60_000)  # refill 1 req/s
    # drain the bucket
    for _ in range(60):
        await b.acquire(1)
    start = time.monotonic()
    await asyncio.gather(b.acquire(1), b.acquire(1))
    elapsed = time.monotonic() - start
    # First acquire waits ~1s for refill; second waits another ~1s
    assert elapsed >= 1.5


async def test_oversize_raises() -> None:
    b = TokenBucket(rpm=60, tpm=1000)
    import pytest

    with pytest.raises(ValueError):
        await b.acquire(2000)
