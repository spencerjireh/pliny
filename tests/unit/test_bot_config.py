import pytest

from pliny.bot.config import parse_allowed_user_ids


def test_parse_empty_returns_empty_set() -> None:
    assert parse_allowed_user_ids("") == frozenset()


def test_parse_whitespace_returns_empty_set() -> None:
    assert parse_allowed_user_ids("   , ,  ") == frozenset()


def test_parse_single_id() -> None:
    assert parse_allowed_user_ids("12345") == frozenset({12345})


def test_parse_csv_with_spaces() -> None:
    assert parse_allowed_user_ids(" 1, 2 ,3 ") == frozenset({1, 2, 3})


def test_parse_dedups() -> None:
    assert parse_allowed_user_ids("7,7,7") == frozenset({7})


def test_parse_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid telegram user id"):
        parse_allowed_user_ids("1,abc,3")
