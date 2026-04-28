import random

RETRY_DELAYS_S: tuple[int, ...] = (1, 4, 16, 64, 256)
MAX_ATTEMPTS = len(RETRY_DELAYS_S) + 1  # 5 retries + 1 initial = 6 total attempts


def next_delay_seconds(attempts: int) -> float | None:
    """Return seconds to wait before retry, or None to give up.

    `attempts` is the count after the failing attempt (i.e. 1 after the first
    failure). Per spec.md: 1s, 4s, 16s, 64s, 256s, then `failed`.
    """
    if attempts < 1 or attempts > len(RETRY_DELAYS_S):
        return None
    base = RETRY_DELAYS_S[attempts - 1]
    jitter = random.uniform(-0.25, 0.25)
    return base * (1.0 + jitter)
