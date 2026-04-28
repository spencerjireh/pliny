from dataclasses import dataclass

import tiktoken

CHUNKER_VERSION = 1
TOKENS_PER_CHUNK = 512
OVERLAP_TOKENS = 64
STRIDE = TOKENS_PER_CHUNK - OVERLAP_TOKENS  # 448
MAX_CHUNKS = 1000

ENCODING = tiktoken.get_encoding("cl100k_base")


@dataclass(frozen=True)
class ChunkPiece:
    index: int
    text: str
    token_count: int


def compute_boundaries(total_tokens: int) -> list[tuple[int, int]]:
    """Return [(start, end), ...] for every window. Last window absorbs the tail."""
    if total_tokens <= 0:
        return []
    boundaries: list[tuple[int, int]] = []
    start = 0
    while True:
        end = min(start + TOKENS_PER_CHUNK, total_tokens)
        boundaries.append((start, end))
        if end == total_tokens:
            break
        start += STRIDE
    return boundaries


def chunk_text(extracted: str) -> tuple[list[ChunkPiece], int]:
    """Slice into 512-token windows with 64-token overlap.

    Returns (pieces, original_count) where original_count is the un-truncated
    count of windows the text would yield. Empty/whitespace -> ([], 0).
    Last window absorbs trailing tokens (no zero-token tail). Truncates to
    MAX_CHUNKS pieces; original_count still reports the full count.
    """
    if not extracted or not extracted.strip():
        return [], 0

    tokens = ENCODING.encode(extracted)
    boundaries = compute_boundaries(len(tokens))
    original_count = len(boundaries)
    capped = boundaries[:MAX_CHUNKS]
    pieces = [
        ChunkPiece(
            index=i,
            text=ENCODING.decode(tokens[s:e]),
            token_count=e - s,
        )
        for i, (s, e) in enumerate(capped)
    ]
    return pieces, original_count
