from uuid import uuid4

from pliny.schemas.search import RRF_K, RankedHit, rrf_fuse


def test_empty_arms() -> None:
    assert rrf_fuse([]) == {}
    assert rrf_fuse([[]]) == {}


def test_single_arm_first_rank() -> None:
    item = uuid4()
    hits = [RankedHit(item_id=item, rank=1, score=0.9)]
    out = rrf_fuse([hits])
    assert out == {item: 1.0 / (RRF_K + 1)}


def test_three_arms_same_top_item_sums() -> None:
    item = uuid4()
    arms = [
        [RankedHit(item_id=item, rank=1, score=0.9)],
        [RankedHit(item_id=item, rank=1, score=0.7)],
        [RankedHit(item_id=item, rank=1, score=0.5)],
    ]
    out = rrf_fuse(arms)
    assert out[item] == 3.0 / (RRF_K + 1)


def test_chunk_arm_multiple_hits_per_item_sum() -> None:
    item = uuid4()
    arm = [
        RankedHit(item_id=item, rank=1, score=0.9, chunk_id=uuid4()),
        RankedHit(item_id=item, rank=2, score=0.8, chunk_id=uuid4()),
        RankedHit(item_id=item, rank=3, score=0.7, chunk_id=uuid4()),
    ]
    out = rrf_fuse([arm])
    expected = sum(1.0 / (RRF_K + r) for r in (1, 2, 3))
    assert out[item] == expected


def test_separate_items_have_separate_scores() -> None:
    a, b = uuid4(), uuid4()
    arm = [
        RankedHit(item_id=a, rank=1, score=0.9),
        RankedHit(item_id=b, rank=2, score=0.8),
    ]
    out = rrf_fuse([arm])
    assert out[a] == 1.0 / (RRF_K + 1)
    assert out[b] == 1.0 / (RRF_K + 2)
