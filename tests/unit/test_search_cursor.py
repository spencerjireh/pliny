from datetime import UTC, datetime
from uuid import uuid4

from pliny.schemas.search import QueryCursor, decode_cursor, encode_cursor


def test_query_mode_round_trip() -> None:
    c = QueryCursor(mode="q", score=0.42, id=uuid4())
    encoded = encode_cursor(c)
    decoded = decode_cursor(encoded)
    assert decoded.mode == "q"
    assert decoded.score == 0.42
    assert decoded.id == c.id


def test_browse_mode_round_trip() -> None:
    now = datetime.now(UTC)
    c = QueryCursor(mode="b", captured_at=now, id=uuid4())
    encoded = encode_cursor(c)
    decoded = decode_cursor(encoded)
    assert decoded.mode == "b"
    assert decoded.captured_at == now
    assert decoded.id == c.id
