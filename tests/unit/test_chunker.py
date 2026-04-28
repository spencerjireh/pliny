from pliny.pipeline.chunk.chunker import (
    ENCODING,
    MAX_CHUNKS,
    OVERLAP_TOKENS,
    STRIDE,
    TOKENS_PER_CHUNK,
    chunk_text,
    compute_boundaries,
)


def test_empty_text() -> None:
    assert chunk_text("") == ([], 0)
    assert chunk_text("   \n  ") == ([], 0)


def test_short_text_one_chunk() -> None:
    text = "hello world this is a short article"
    pieces, original = chunk_text(text)
    assert original == 1
    assert len(pieces) == 1
    assert pieces[0].index == 0
    assert pieces[0].text == text
    assert pieces[0].token_count == len(ENCODING.encode(text))


def test_multi_chunk_count_matches_formula() -> None:
    # Build text whose token length we control. Repeat a 1-token word.
    word = "alpha "
    n_tokens_target = 2000
    text = word * (n_tokens_target * 2)  # over-generate then trim
    tokens = ENCODING.encode(text)[:n_tokens_target]
    text = ENCODING.decode(tokens)
    pieces, original = chunk_text(text)

    # Expected: ceil((N - TOKENS_PER_CHUNK) / STRIDE) + 1, with last absorbing the remainder.
    expected = 1 + ((n_tokens_target - TOKENS_PER_CHUNK + STRIDE - 1) // STRIDE)
    assert original == expected
    assert len(pieces) == expected
    for i, p in enumerate(pieces):
        assert p.index == i


def test_overlap_correctness() -> None:
    word = "alpha "
    tokens = ENCODING.encode(word * 4000)[:2000]
    text = ENCODING.decode(tokens)
    pieces, _ = chunk_text(text)
    assert len(pieces) >= 2

    # Last OVERLAP_TOKENS of piece i should equal first OVERLAP_TOKENS of piece i+1.
    enc0 = ENCODING.encode(pieces[0].text)
    enc1 = ENCODING.encode(pieces[1].text)
    assert enc0[-OVERLAP_TOKENS:] == enc1[:OVERLAP_TOKENS]


def test_last_window_no_zero_tail() -> None:
    word = "alpha "
    tokens = ENCODING.encode(word * 2000)[: TOKENS_PER_CHUNK + 10]
    text = ENCODING.decode(tokens)
    pieces, _ = chunk_text(text)
    assert pieces[-1].token_count > 0
    # Last window starts at STRIDE (since we have 522 tokens > 512).
    assert pieces[-1].token_count == 522 - STRIDE


def test_compute_boundaries_overflow() -> None:
    # Pure boundary math: avoids tokenizer round-trip artifacts.
    n_tokens = TOKENS_PER_CHUNK + STRIDE * (MAX_CHUNKS + 5)
    bounds = compute_boundaries(n_tokens)
    assert len(bounds) == 1 + ((n_tokens - TOKENS_PER_CHUNK + STRIDE - 1) // STRIDE)
    assert len(bounds) > MAX_CHUNKS


def test_compute_boundaries_zero() -> None:
    assert compute_boundaries(0) == []


def test_compute_boundaries_short() -> None:
    assert compute_boundaries(10) == [(0, 10)]
    assert compute_boundaries(TOKENS_PER_CHUNK) == [(0, TOKENS_PER_CHUNK)]
