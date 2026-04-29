def parse_allowed_user_ids(raw: str) -> frozenset[int]:
    """Parse a comma-separated list of Telegram user IDs.

    Empty or whitespace-only input returns an empty set: the bot is fail-closed,
    so an unset env var means every message is dropped.
    """
    out: set[int] = set()
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.add(int(s))
        except ValueError as e:
            raise ValueError(f"invalid telegram user id: {s!r}") from e
    return frozenset(out)
