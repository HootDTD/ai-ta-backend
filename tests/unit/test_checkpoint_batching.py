from __future__ import annotations

from indexing.checkpoint_indexer import group_pages, plan_batches


def _pair(text, page):
    return (text, {"page_number": page, "chunk_type": "body"})


def test_group_pages_splits_null_and_orders_by_page():
    pairs = [
        _pair("p2a", 2),
        _pair("p1a", 1),
        _pair("p1b", 1),
        _pair("nopage", None),
        _pair("p3a", 3),
    ]
    page_groups, null_items = group_pages(pairs)

    assert [pg.page_number for pg in page_groups] == [1, 2, 3]
    assert [len(pg.items) for pg in page_groups] == [2, 1, 1]
    assert [t for t, _ in null_items] == ["nopage"]


def test_plan_batches_packs_whole_pages_under_size():
    pairs = [_pair("a", 1), _pair("b", 1), _pair("c", 2), _pair("d", 3)]
    page_groups, _ = group_pages(pairs)

    batches = list(plan_batches(page_groups, batch_size=3, after_page=0))

    # page1 has 2 chunks; adding page2 (1) -> 3 (==size) ok; page3 starts new batch
    assert [[pg.page_number for pg in b] for b in batches] == [[1, 2], [3]]


def test_plan_batches_skips_pages_at_or_below_pointer():
    pairs = [_pair("a", 1), _pair("b", 2), _pair("c", 3)]
    page_groups, _ = group_pages(pairs)

    batches = list(plan_batches(page_groups, batch_size=10, after_page=2))

    assert [[pg.page_number for pg in b] for b in batches] == [[3]]


def test_plan_batches_oversized_single_page_is_own_batch():
    pairs = [_pair(f"x{i}", 1) for i in range(5)] + [_pair("y", 2)]
    page_groups, _ = group_pages(pairs)

    batches = list(plan_batches(page_groups, batch_size=3, after_page=0))

    assert [[pg.page_number for pg in b] for b in batches] == [[1], [2]]
