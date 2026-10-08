"""Regression baseline for the group-feed crawl.

The fixture is a synthetic payload set (structure from a real group feed,
every id, name and text replaced). The expected file is the extractor's
output on it before the search work started; any change to extraction or
the crawl loop must keep these tests green.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from fbtool import scrape as S
from fbtool.config import Config, Group
from fakes import FakePage

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def feed():
    fx = load_fixture("feed_group")
    group = Group(slug=fx["group_slug"], name="Sample group")
    expected = json.loads((FIXTURES / "feed_group.expected.json").read_text())
    return fx["bodies"], group, expected


def baseline_view(posts):
    """The fields the baseline snapshot recorded; fields added later
    (attachment, comment_count) are tested separately."""
    keys = ("id", "author", "text", "posted_at", "permalink", "group_slug")
    return sorted(({k: p[k] for k in keys} for p in posts), key=lambda p: p["id"])


def cfg_for(group):
    return Config(groups=[group], max_scrolls=40, scroll_pause_ms=0)


def run_crawl(bodies, group, cutoff, known=frozenset(), repeat=0):
    texts = [json.dumps(b) for b in bodies]
    batches = [[t] for t in texts[1:]] + [[texts[-1]]] * repeat
    responses = []
    page = FakePage(responses, embedded=[texts[0]], batches=batches)
    posts = S.scrape_group(page, group, cutoff, cfg_for(group), responses, set(known))
    return posts, page


def test_extraction_matches_baseline(feed):
    bodies, group, expected = feed
    posts = {}
    for doc in bodies:
        roots = []
        S._collect_roots(doc, roots)
        for r in roots:
            p = S._story_to_post(r, group)
            if p:
                S._merge(posts, p)
    assert baseline_view(posts.values()) == expected


def test_baseline_shape(feed):
    _, group, expected = feed
    assert len(expected) == 5
    assert all(p["posted_at"] and p["author"] for p in expected)
    # four posts live under the feed's own path; one is a shared post that
    # only carries the original's pfbid permalink
    own = [p for p in expected if f"facebook.com/groups/{group.slug}/posts/" in p["permalink"]]
    assert len(own) == 4
    assert sum("[top comments]" in p["text"] for p in expected) == 4
    assert max(p["text"].count("\n  > ") for p in expected) == 2


def test_crawl_returns_baseline(feed):
    bodies, group, expected = feed
    posts, page = run_crawl(bodies, group, datetime(2000, 1, 1))
    assert baseline_view(posts) == expected
    assert page.url == group.feed_url


def test_crawl_stops_past_cutoff(feed):
    bodies, group, _ = feed
    posts, page = run_crawl(bodies, group, datetime(2100, 1, 1), repeat=20)
    assert posts == []
    # every batch is past the window; the loop stops after PAST_WINDOW_LIMIT
    # batches that brought new posts, then only stale scrolls remain
    assert page.scrolls < 40


def test_crawl_stops_on_known_posts(feed):
    bodies, group, expected = feed
    known = {p["id"] for p in expected}
    _, page = run_crawl(bodies, group, datetime(2000, 1, 1), known=known, repeat=20)
    assert page.scrolls <= len(bodies) - 1 + S.KNOWN_STREAK_LIMIT


def test_crawl_stops_when_nothing_new(feed):
    bodies, group, _ = feed
    _, page = run_crawl(bodies[:1], group, datetime(2000, 1, 1))
    assert page.scrolls == S.STALE_SCROLL_LIMIT
