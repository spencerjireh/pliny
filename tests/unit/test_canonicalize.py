import pytest

from pliny.canonicalize import canonicalize


def test_lowercase_scheme_and_host() -> None:
    assert canonicalize("HTTPS://Example.COM/foo") == "https://example.com/foo"


def test_drop_default_port_http() -> None:
    assert canonicalize("http://example.com:80/x") == "http://example.com/x"


def test_drop_default_port_https() -> None:
    assert canonicalize("https://example.com:443/x") == "https://example.com/x"


def test_keep_non_default_port() -> None:
    assert canonicalize("https://example.com:8443/x") == "https://example.com:8443/x"


def test_drop_fragment() -> None:
    assert canonicalize("https://example.com/x#section-1") == "https://example.com/x"


def test_strip_tracking_params() -> None:
    url = "https://example.com/?utm_source=x&utm_medium=y&utm_campaign=z&fbclid=q&gclid=g"
    assert canonicalize(url) == "https://example.com/"


def test_keep_non_tracking_params() -> None:
    assert canonicalize("https://example.com/?id=1") == "https://example.com/?id=1"


def test_sort_query_params() -> None:
    assert canonicalize("https://example.com/?b=2&a=1&c=3") == "https://example.com/?a=1&b=2&c=3"


def test_strip_trailing_slash_on_path() -> None:
    assert canonicalize("https://example.com/foo/") == "https://example.com/foo"


def test_keep_root_slash() -> None:
    assert canonicalize("https://example.com/") == "https://example.com/"


def test_strip_repeated_trailing_slashes() -> None:
    assert canonicalize("https://example.com/foo///") == "https://example.com/foo"


def test_full_composition() -> None:
    src = "HTTPS://Example.COM:443/foo/?utm_source=x&b=2&a=1#frag"
    assert canonicalize(src) == "https://example.com/foo?a=1&b=2"


def test_repeated_keys_preserved_and_sorted() -> None:
    src = "https://example.com/?b=2&a=2&a=1"
    assert canonicalize(src) == "https://example.com/?a=1&a=2&b=2"


def test_blank_value_kept() -> None:
    src = "https://example.com/?q="
    assert canonicalize(src) == "https://example.com/?q="


def test_userinfo_preserved() -> None:
    src = "https://user:pass@Example.COM/x"
    assert canonicalize(src) == "https://user:pass@example.com/x"


def test_missing_scheme_raises() -> None:
    with pytest.raises(ValueError):
        canonicalize("example.com/foo")


def test_missing_host_raises() -> None:
    with pytest.raises(ValueError):
        canonicalize("https:///foo")


def test_strip_surrounding_whitespace() -> None:
    assert canonicalize("  https://example.com/  ") == "https://example.com/"
