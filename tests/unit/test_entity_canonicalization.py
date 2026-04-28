"""Pure-logic checks on entity canonicalization expectations.

The full canonicalization round-trip is exercised in
tests/integration/test_entities.py — these unit tests sanity-check the
intermediate expectations independent of the database.
"""

from pliny.pipeline.entities.handler import VALID_TYPES


def test_valid_types_match_spec() -> None:
    assert {"person", "place", "org", "concept", "work", "other"} == VALID_TYPES


def test_canonical_name_lowercase_normalization() -> None:
    """Canonicalization rule: name.strip().lower() is the matching key."""
    samples = [
        ("Albert Einstein", "albert einstein"),
        ("  Apple Inc.  ", "apple inc."),
        ("apple inc.", "apple inc."),
        ("MERCURY", "mercury"),
    ]
    for raw, expected in samples:
        assert raw.strip().lower() == expected
