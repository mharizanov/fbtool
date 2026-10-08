import base64
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from fakes import FakePage, bodies_of
from fbtool import db, search as SR
from fbtool.config import Config, Group
from fbtool.scrape import _collect_roots
from fbtool.summarize import build_corpus

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 8, 12, 0, 0)


def search_fixture():
    return json.loads((FIXTURES / "search_posts.json").read_text())["bodies"]


def make_cfg(tmp_path, groups=(), **kw):
    return Config(groups=list(groups), db_path=tmp_path / "t.db", runs_dir=tmp_path / "runs",
                  max_scrolls=40, scroll_pause_ms=0, **kw)


def hits(bodies, cfg):
    resolver = SR.OriginResolver(cfg)
    posts = {}
    for doc in bodies:
        roots = []
        _collect_roots(doc, roots)
        for r in roots:
            p = SR._story_to_hit(r, resolver)
            if p:
                SR._merge_hit(posts, p)
    return posts


def story(pid, text, url=None, comments=(), count=None, group=None, ts=1759900000):
    """A minimal synthetic story rendering."""
    s = {"comet_sections": {}, "post_id": pid, "creation_time": ts,
         "message": {"text": text}, "actors": [{"name": "Sample Author"}]}
    if url:
        s["wwwURL"] = url
    if count is not None:
        s["feedback"] = {"comment_rendering_instance": {"comments": {"total_count": count}}}
    if comments:
        s["comments"] = [{"__typename": "Comment", "body": {"text": c},
                          "author": {"name": "Commenter"}} for c in comments]
    if group:
        s["to"] = {"__typename": "Group", "id": group[0], "name": group[1],
                   "url": f"https://www.facebook.com/groups/{group[2]}/"}
    return s


def run(cfg, page_script, queries=("sample query",), **kw):
    responses = []
    page = FakePage(responses, script=page_script)
    code, run_dir, record = SR.search(cfg, list(queries), session=(page, responses),
                                      now=kw.pop("now", NOW), **kw)
    return code, run_dir, record, page


def script_for(bodies):
    texts = bodies_of(bodies)
    return {SR.SEARCH_URL: (texts[:1], [[t] for t in texts[1:]])}


# ---------------------------------------------------------------- origins

def test_origins_unconfigured(tmp_path):
    posts = hits(search_fixture(), make_cfg(tmp_path))
    assert len(posts) == 10
    kinds = {p["id"]: (p["origin"]["kind"], p["group_slug"]) for p in posts.values()}
    # group posts are keyed by the group's numeric id, also when the
    # permalink uses the vanity name
    assert kinds["1798863552866166"] == ("group", "1761193813232214")
    assert posts["1798863552866166"]["origin"]["url"] == "https://www.facebook.com/groups/samplefeed2/"
    assert kinds["8003265486201501"] == ("group", "6244813330590838")
    # Page/profile posts with pfbid permalinks: keyed by path
    assert kinds["292888230352967"] == ("author", "samplefeed4")
    # no URL anywhere: unknown origin, id-only permalink
    assert kinds["248364916984706055"] == ("unknown", "search")
    assert posts["248364916984706055"]["permalink"] == "https://www.facebook.com/248364916984706055"


@pytest.mark.parametrize("slug", ["1761193813232214", "samplefeed2", "SampleFeed2"])
def test_origin_normalized_to_configured_slug(tmp_path, slug):
    cfg = make_cfg(tmp_path, [Group(slug=slug, name="Configured")])
    p = hits(search_fixture(), cfg)["1798863552866166"]
    assert p["group_slug"] == slug
    assert p["origin"]["name"] == "Configured"


def test_attachment_and_comment_count():
    s = story("111", "", url="https://www.facebook.com/groups/9/posts/111/", count=7)
    s["attachments"] = [{"media": {"title_with_entities": {"text": "Link title"},
                                   "description": {"text": "Link description"}}}]
    p = hits([{"data": s}], Config(groups=[]))["111"]
    assert p["attachment"] == "Link title — Link description"
    assert p["comment_count"] == 7


# ---------------------------------------------------------------- filters

def test_filters_param_and_keep():
    f = SR.Filters(recent=True, since=date(2026, 1, 1), until=date(2026, 3, 31))
    decoded = json.loads(base64.b64decode(f.fb_param()))
    assert set(decoded) == {"recent_posts:0", "rp_creation_time:0"}
    args = json.loads(json.loads(decoded["rp_creation_time:0"])["args"])
    assert args["start_day"] == "2026-1-1" and args["end_day"] == "2026-3-31"
    assert f.keep("2026-02-01T10:00:00") and not f.keep("2025-12-31T23:00:00")
    assert f.keep(None)
    assert SR.Filters().fb_param() is None
    assert SR.search_url("a b", SR.Filters()) == SR.SEARCH_URL + "a%20b"


# ---------------------------------------------------------------- runs

def test_search_run_writes_corpus_and_budget(tmp_path):
    cfg = make_cfg(tmp_path)
    code, run_dir, record, page = run(cfg, script_for(search_fixture()))
    assert code == SR.EXIT_OK
    assert len(page.gotos) == 1 and page.gotos[0].startswith(SR.SEARCH_URL)
    assert len(record["posts"]) == 10
    assert record["queries"][0]["source"] == "live" and record["queries"][0]["hits"] == 10
    assert record["budget"]["used_today"]["page_loads"] == 1
    assert record["budget"]["used_today"]["queries"] == 1
    assert record["budget"]["used_today"]["scrolls"] == page.scrolls
    corpus = (run_dir / "corpus.md").read_text()
    assert all(f"--- post {p['id']} |" in corpus for p in record["posts"])
    assert json.loads((run_dir / "run.json").read_text())["posts"] == record["posts"]
    assert run_dir.name.startswith("sample-query-20261008-120000")
    con = db.connect(cfg.db_path)
    assert con.execute("SELECT count(*) FROM search_hits").fetchone()[0] == 10
    assert con.execute("SELECT name FROM sources WHERE slug = '1761193813232214'").fetchone()[0]


def test_cache_within_24h_skips_facebook(tmp_path):
    cfg = make_cfg(tmp_path)
    run(cfg, script_for(search_fixture()))
    code, _, record, page = run(cfg, script_for(search_fixture()), now=NOW + timedelta(hours=2))
    assert code == SR.EXIT_OK and page.gotos == []
    assert record["queries"][0]["source"] == "cache" and len(record["posts"]) == 10
    assert record["budget"]["used_today"]["page_loads"] == 1
    _, _, record, page = run(cfg, script_for(search_fixture()), now=NOW + timedelta(hours=25))
    assert record["queries"][0]["source"] == "live" and len(page.gotos) == 1


def test_budget_refused_up_front(tmp_path):
    cfg = make_cfg(tmp_path, search_budget={"page_loads": 2, "scrolls": 300, "queries": 20})
    code, run_dir, record, page = run(cfg, script_for(search_fixture()), queries=("a", "b", "c"))
    assert code == SR.EXIT_BUDGET and run_dir is None and page.gotos == []
    assert "page_loads 0+3 > 2" in record["error"]
    # expand page loads count too
    code, _, record, page = run(cfg, script_for(search_fixture()), queries=("a",), expand=2)
    assert code == SR.EXIT_BUDGET and page.gotos == []


def test_max_results_stops_scrolling(tmp_path):
    cfg = make_cfg(tmp_path)
    docs = [{"data": story(str(1000 + i), f"post {i}", url=f"https://www.facebook.com/groups/9/posts/{1000 + i}/")}
            for i in range(10)]
    code, _, record, page = run(cfg, script_for(docs), max_results=3)
    assert len(record["posts"]) == 3
    assert page.scrolls == 2  # embedded doc + 2 scroll batches reach 3 posts


def test_expand_fetches_comments(tmp_path):
    cfg = make_cfg(tmp_path)
    url = "https://www.facebook.com/groups/9/posts/2000/"
    hit = {"data": story("2000", "question", url=url, count=3)}
    quiet = {"data": story("2001", "no comments", url="https://www.facebook.com/groups/9/posts/2001/", count=0)}
    full = {"data": story("2000", "question", url=url, count=3, comments=["first answer", "second"])}
    script = {SR.SEARCH_URL: (bodies_of([hit, quiet]), []), url: (bodies_of([full]), [])}
    code, run_dir, record, page = run(cfg, script, expand=5)
    assert page.gotos[1:] == [url]  # only the post with comments is opened
    assert record["expand"]["done"] == 1
    assert "first answer" in (run_dir / "corpus.md").read_text()
    con = db.connect(cfg.db_path)
    assert con.execute("SELECT count(*) FROM post_versions WHERE post_id = '2000'").fetchone()[0] == 1
    assert record["budget"]["used_today"]["page_loads"] == 2


def test_checkpoint_aborts_run(tmp_path):
    cfg = make_cfg(tmp_path)
    responses = []
    page = FakePage(responses, script=script_for(search_fixture()),
                    final_url="https://www.facebook.com/checkpoint/123/")
    code, run_dir, record = SR.search(cfg, ["a", "b"], session=(page, responses), now=NOW)
    assert code == SR.EXIT_CHECKPOINT
    assert "checkpoint" in record["error"].lower()
    assert len(page.gotos) == 1  # second query never attempted
    assert json.loads((run_dir / "run.json").read_text())["error"]


# ---------------------------------------------------------------- feeds vs search

def test_feed_scrape_takes_ownership(tmp_path):
    con = db.connect(tmp_path / "t.db")
    base = {"id": "3000", "author": "A", "text": "t", "posted_at": "2026-10-01T10:00:00",
            "scraped_at": "2026-10-02T10:00:00"}
    db.upsert_post(con, {**base, "group_slug": "555",
                         "permalink": "https://www.facebook.com/groups/555/posts/3000/"})
    # a search re-sighting under another known origin never moves it
    db.upsert_post(con, {**base, "group_slug": "other", "permalink": "x"})
    assert con.execute("SELECT group_slug FROM posts").fetchone()[0] == "555"
    db.upsert_post(con, {**base, "group_slug": "mygroup",
                         "permalink": "https://www.facebook.com/groups/mygroup/posts/3000/"},
                   owner=True)
    row = con.execute("SELECT group_slug, permalink FROM posts").fetchone()
    assert tuple(row) == ("mygroup", "https://www.facebook.com/groups/mygroup/posts/3000/")
    assert db.known_post_ids(con, "mygroup") == {"3000"}


def test_side_fields_do_not_count_as_updates(tmp_path):
    con = db.connect(tmp_path / "t.db")
    post = {"id": "4000", "group_slug": "g", "author": "A", "text": "t",
            "posted_at": "2026-10-01T10:00:00", "permalink": "p", "scraped_at": "2026-10-02T10:00:00"}
    db.upsert_post(con, post)
    assert db.upsert_post(con, {**post, "attachment": "title", "comment_count": 4}) == "unchanged"
    assert tuple(con.execute("SELECT attachment, comment_count FROM posts").fetchone()) == ("title", 4)


def test_search_hits_stay_out_of_the_digest(tmp_path):
    cfg = make_cfg(tmp_path, [Group(slug="1761193813232214", name="Configured")])
    run(cfg, script_for(search_fixture()))
    corpus, n = build_corpus(cfg, datetime(2000, 1, 1))
    in_configured = [p for p in hits(search_fixture(), cfg).values()
                     if p["group_slug"] == "1761193813232214"]
    assert n == len(in_configured) == 3


def test_permalink_php_and_reel_origins():
    php = story("5001", "page post", url="https://www.facebook.com/permalink.php?story_fbid=pfbid0Abc&id=61550000000001&rdid=x")
    reel = story("5002", "a reel", url="https://www.facebook.com/reel/1234567890/?s=fb")
    reel["actors"] = [{"name": "Reel Maker", "id": "100000000000009",
                       "url": "https://www.facebook.com/reelmaker"}]
    posts = hits([{"a": php}, {"b": reel}], Config(groups=[]))
    p = posts["5001"]
    assert p["permalink"] == "https://www.facebook.com/permalink.php?story_fbid=pfbid0Abc&id=61550000000001"
    assert (p["origin"]["kind"], p["group_slug"]) == ("author", "61550000000001")
    assert p["origin"]["url"] == "https://www.facebook.com/profile.php?id=61550000000001"
    r = posts["5002"]
    assert r["permalink"] == "https://www.facebook.com/reel/1234567890/"
    assert (r["origin"]["kind"], r["group_slug"], r["origin"]["quality"]) == ("author", "reelmaker", 0.5)


def test_group_node_used_when_permalink_has_no_path():
    s = story("5003", "group reel", url="https://www.facebook.com/reel/99999999/",
              group=("777000000000001", "Sample Group", "samplegroup"))
    p = hits([{"a": s}], Config(groups=[]))["5003"]
    assert (p["origin"]["kind"], p["group_slug"], p["origin"]["name"]) == ("group", "777000000000001", "Sample Group")


def test_better_rendering_replaces_actor_guess():
    bare = story("5004", "text")
    bare["actors"] = [{"name": "Someone", "id": "100000000000010"}]
    full = story("5004", "text", url="https://www.facebook.com/groups/samplegroup/posts/5004/",
                 group=("777000000000001", "Sample Group", "samplegroup"))
    p = hits([{"a": bare}, {"b": full}], Config(groups=[]))["5004"]
    assert p["group_slug"] == "777000000000001"
    assert p["permalink"] == "https://www.facebook.com/groups/samplegroup/posts/5004/"


def test_known_origin_replaces_unknown(tmp_path):
    con = db.connect(tmp_path / "t.db")
    base = {"id": "6000", "author": "A", "text": "t", "posted_at": "2026-10-01T10:00:00",
            "scraped_at": "2026-10-02T10:00:00"}
    db.upsert_post(con, {**base, "group_slug": db.UNKNOWN_ORIGIN, "permalink": "https://www.facebook.com/6000"})
    db.upsert_post(con, {**base, "group_slug": "777", "permalink": "https://www.facebook.com/groups/777/posts/6000/"})
    assert tuple(con.execute("SELECT group_slug, permalink FROM posts").fetchone()) == (
        "777", "https://www.facebook.com/groups/777/posts/6000/")


def test_rank_follows_result_order(tmp_path):
    cfg = make_cfg(tmp_path)
    edges = [{"node": story(str(7000 + i), f"r{i}", url=f"https://www.facebook.com/groups/9/posts/{7000 + i}/")}
             for i in range(4)]
    code, _, record, _ = run(cfg, script_for([{"data": {"results": {"edges": edges}}}]))
    assert [p["id"] for p in record["posts"]] == ["7000", "7001", "7002", "7003"]
    assert [p["matched"][0]["rank"] for p in record["posts"]] == [1, 2, 3, 4]
