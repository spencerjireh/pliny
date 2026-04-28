from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "mc_eid",
        "mc_cid",
        "ref",
        "ref_src",
        "igshid",
        "_ga",
        "yclid",
        "ymclid",
    }
)

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def canonicalize(url: str) -> str:
    parts = urlsplit(url.strip())
    if not parts.scheme:
        raise ValueError(f"missing scheme: {url!r}")
    if not parts.hostname:
        raise ValueError(f"missing host: {url!r}")

    scheme = parts.scheme.lower()
    host = parts.hostname.lower()

    netloc = host
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    if parts.port is not None and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{parts.port}"

    pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS
    ]
    pairs.sort()
    query = urlencode(pairs)

    path = parts.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, query, ""))
