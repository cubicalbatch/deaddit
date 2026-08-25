"""A3 feed-SQL regression: subdeaddit feeds are SQL-paginated and deterministic.

Guards the Phase A3 cutover (Resolution 3): feeds must serve stable
``created_at DESC, id DESC`` pages straight from SQL — no full-table loads,
no random shuffles, and never a NameError from removed pagination helpers.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from deaddit.models import Post


def _backfill_posts(sub: str, count: int) -> list[Post]:
    """Posts with strictly increasing timestamps so ordering is unambiguous."""
    # Timestamps in 2027 outrank the seeded_db rows' utcnow stamps, so the
    # expected page windows below are fully determined by these posts.
    base = datetime(2027, 1, 1, 12, 0, 0)
    return [
        Post(
            title=f"Feed post {i:02d}",
            content="feed sql fixture",
            user="alice",
            subdeaddit_name=sub,
            model="test-model",
            created_at=base + timedelta(minutes=i),
        )
        for i in range(count)
    ]


def _page_titles(html: str) -> list[str]:
    """Titles as rendered on the page, in display order."""
    return re.findall(r"Feed post (\d{2})", html)


def test_subdeaddit_feed_pages_are_deterministic_windows(app, client, db_session, seeded_db):
    db_session.add_all(_backfill_posts("testsub", 12))
    db_session.commit()

    # Subdeaddit feeds show 10 per page; page 1 must be the 10 newest.
    first = client.get("/d/testsub")
    assert first.status_code == 200
    page_one = _page_titles(first.get_data(as_text=True))
    assert page_one == [f"{i:02d}" for i in range(11, 1, -1)]

    second = client.get("/d/testsub", query_string={"page": 2})
    assert second.status_code == 200
    page_two = _page_titles(second.get_data(as_text=True))
    assert page_two == ["01", "00"]

    # Windows partition the feed: no overlap, nothing lost.
    assert set(page_one).isdisjoint(page_two)

    # Repeated requests are byte-stable (no shuffle underneath).
    assert client.get("/d/testsub").get_data() == first.data
