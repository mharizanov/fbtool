"""Search public Facebook posts by keyword and hand the results to an LLM.

`fbtool search "q1" "q2" …` runs each query through facebook.com/search/posts
in one browser session, captures the result payloads the same way the feed
scraper does, and writes a run directory:

  runs/<slug>-<timestamp>/run.json   machine-readable: queries, hits, origins, budget
  runs/<slug>-<timestamp>/corpus.md  one block per post, ready to read or prompt with

The caller (a person, or a coding agent such as Claude Code) plans the
queries and judges relevance; this module does no LLM calls. Search is
rate-limited harder than feed reading, so every page load and scroll counts
against a daily budget (config `search_budget`).
"""

import base64
import json
import re
import unicodedata
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from playwright.sync_api import sync_playwright

from . import db
from .config import Config
from .scrape import (POST_URL_RE, STALE_SCROLL_LIMIT, CheckpointError, _collect_roots_ordered,
                     _crawl, _dig, _merge,
                     _story_fields, _walk, ensure_logged_in, open_context)

SEARCH_URL = "https://www.facebook.com/search/posts?q="
CACHE_HOURS = 24
GROUP_URL_RE = re.compile(r"facebook\.com/groups/([^/?#]+)")
AUTHOR_URL_RE = re.compile(r"facebook\.com/([^/?#]+)/posts/")
# Search results also link posts these ways (feeds don't, so these stay
# out of scrape.POST_URL_RE): Pages/profiles without a vanity name, reels.
PERMALINK_PHP_RE = re.compile(r"facebook\.com/permalink\.php\?")
REEL_RE = re.compile(r"facebook\.com/reel/\d+")
PROFILE_URL_RE = re.compile(r"facebook\.com/(?:profile\.php\?id=(\d+)|([^/?#]+))")

# Exit codes, stable for scripted callers.
EXIT_OK, EXIT_NO_HITS, EXIT_BUDGET, EXIT_CHECKPOINT = 0, 1, 2, 3


class BudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class Filters:
    recent: bool = False
    since: date | None = None
    until: date | None = None

    def canonical(self) -> str:
        return json.dumps({"recent": self.recent,
                           "since": self.since and self.since.isoformat(),
                           "until": self.until and self.until.isoformat()}, sort_keys=True)

    def fb_param(self) -> str | None:
        """Facebook's own `filters=` value: base64 of a JSON map whose
        values are themselves JSON strings (verified 2026-10-08)."""
        f = {}
        if self.recent:
            f["recent_posts:0"] = json.dumps({"name": "recent_posts", "args": ""})
        if self.since or self.until:
            s, u = self.since or date(2004, 1, 1), self.until or date.today()
            args = {"start_year": str(s.year), "start_month": f"{s.year}-{s.month}",
                    "start_day": f"{s.year}-{s.month}-{s.day}",
                    "end_year": str(u.year), "end_month": f"{u.year}-{u.month}",
                    "end_day": f"{u.year}-{u.month}-{u.day}"}
            f["rp_creation_time:0"] = json.dumps({"name": "creation_time", "args": json.dumps(args)})
        if not f:
            return None
        return base64.b64encode(json.dumps(f, separators=(",", ":")).encode()).decode()

    def keep(self, posted_at: str | None) -> bool:
        if posted_at is None:
            return True
        day = posted_at[:10]
        return ((not self.since or day >= self.since.isoformat())
                and (not self.until or day <= self.until.isoformat()))

    def describe(self) -> str:
        parts = ["recent"] if self.recent else []
        if self.since:
            parts.append(f"since {self.since}")
        if self.until:
            parts.append(f"until {self.until}")
        return ", ".join(parts) or "none"


def search_url(query: str, filters: Filters) -> str:
    url = SEARCH_URL + quote(query)
    fb = filters.fb_param()
    return url + "&filters=" + quote(fb) if fb else url


# ---------------------------------------------------------------- origins

class OriginResolver:
    """Works out where a search hit lives. A post in a configured group or
    Page gets that feed's slug, so it is the same row a feed scrape writes;
    anything else is keyed by the group's numeric id (or the vanity name
    when the payload has no id) or the author/Page path."""

    def __init__(self, cfg: Config):
        self.configured = {g.slug.lower(): g for g in cfg.groups}

    def _configured(self, *keys):
        for k in keys:
            if k and k.lower() in self.configured:
                return self.configured[k.lower()]
        return None

    def resolve(self, story: dict, url: str | None, author: str | None) -> dict:
        m = GROUP_URL_RE.search(url or "")
        if not m and not AUTHOR_URL_RE.search(url or "") and not PERMALINK_PHP_RE.search(url or ""):
            # No group or author path in the permalink (reel, no URL): the
            # story's Group node says where it was posted, else its actor.
            node = next((d for d in _walk(story) if d.get("__typename") == "Group"
                         and isinstance(d.get("id"), str) and d.get("url")), None)
            if node:
                m = GROUP_URL_RE.search(node["url"])
        if m:
            key = m.group(1)
            node = next((d for d in _walk(story)
                         if d.get("__typename") == "Group" and isinstance(d.get("id"), str)
                         and (d["id"] == key or f"/groups/{key}/" in (d.get("url") or ""))), None)
            gid = node["id"] if node else None
            name = node.get("name") if node else None
            node_url = (node or {}).get("url")
            vanity = GROUP_URL_RE.search(node_url or "")
            g = self._configured(gid, key, vanity and vanity.group(1))
            if g:
                return {"slug": g.slug, "kind": "group", "name": g.name,
                        "url": f"https://www.facebook.com/{g.path}/", "quality": 3}
            return {"slug": gid or key, "kind": "group", "name": name,
                    "url": node_url or f"https://www.facebook.com/groups/{key}/",
                    "quality": 2 if node else 1}
        m = AUTHOR_URL_RE.search(url or "")
        key = m.group(1) if m and m.group(1) not in ("groups", "search") else None
        if not key and PERMALINK_PHP_RE.search(url or ""):
            key = (parse_qs(urlsplit(url).query).get("id") or [None])[0]
        from_actor = not key
        if from_actor:
            actors = _dig(story, "actors", list)
            actor = actors[0] if actors and isinstance(actors[0], dict) else {}
            pm = PROFILE_URL_RE.search(actor.get("url") or "")
            key = pm and (pm.group(1) or pm.group(2)) or actor.get("id")
        if key:
            g = self._configured(key)
            if g:
                return {"slug": g.slug, "kind": g.kind, "name": g.name,
                        "url": f"https://www.facebook.com/{g.path}/", "quality": 3}
            return {"slug": key, "kind": "author", "name": author,
                    "url": (f"https://www.facebook.com/profile.php?id={key}" if key.isdigit()
                            else f"https://www.facebook.com/{key}"),
                    # an actor guess yields to any rendering with a real permalink
                    "quality": 0.5 if from_actor else 1}
        return {"slug": db.UNKNOWN_ORIGIN, "kind": "unknown", "name": None, "url": None, "quality": 0}


def _hit_urls(story: dict) -> list[str]:
    """Post permalinks in _walk order, normalized: query stripped, except
    permalink.php, whose story_fbid/id parameters are the address."""
    urls = []
    for d in _walk(story):
        for key in ("wwwURL", "url", "permalink_url", "permalink"):
            v = d.get(key)
            if not isinstance(v, str):
                continue
            if POST_URL_RE.search(v):
                urls.append(v.split("?")[0])
            elif PERMALINK_PHP_RE.search(v):
                q = parse_qs(urlsplit(v).query)
                keep = {k: q[k][0] for k in ("story_fbid", "id") if k in q}
                if "story_fbid" in keep:
                    urls.append("https://www.facebook.com/permalink.php?" + urlencode(keep))
            elif REEL_RE.search(v):
                urls.append("https://www." + REEL_RE.search(v).group(0) + "/")
    return urls


def _story_to_hit(story: dict, resolver: OriginResolver) -> dict | None:
    post = _story_fields(story)
    if post is None:
        return None
    urls = _hit_urls(story)
    # The post's own URL contains its id; a shared post also carries the
    # original's URL, which doesn't.
    url = next((u for u in urls if post["id"] in u), urls[0] if urls else None)
    origin = resolver.resolve(story, url, post["author"])
    post["permalink"] = url or f"https://www.facebook.com/{post['id']}"
    post["group_slug"] = origin["slug"]
    post["origin"] = origin
    return post


def _merge_hit(posts: dict, post: dict) -> bool:
    """_merge, plus: keep the rendering that best identifies the origin
    (several partial renderings of one post arrive, some without a URL)."""
    is_new = _merge(posts, post)
    old = posts[post["id"]]
    if not is_new and post["origin"]["quality"] > old["origin"]["quality"]:
        for key in ("origin", "group_slug", "permalink"):
            old[key] = post[key]
    return is_new


# ---------------------------------------------------------------- run

def slugify(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^\w]+", "-", t).strip("-")[:50] or "search"


@contextmanager
def _browser(cfg: Config):
    ensure_logged_in(cfg)
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        responses: list = []
        page.on("response", lambda r: responses.append(r) if "/api/graphql" in r.url else None)
        try:
            yield page, responses
        finally:
            ctx.close()


def _crawl_search(page, responses, cfg, query, filters, resolver, max_results, max_scrolls):
    state = {"stale": 0}

    def on_batch(_times, _touched, new_ids, posts) -> bool:
        if len(posts) >= max_results:
            return True
        state["stale"] = 0 if new_ids else state["stale"] + 1
        return state["stale"] >= STALE_SCROLL_LIMIT

    return _crawl(page, search_url(query, filters), cfg, responses,
                  lambda root: _story_to_hit(root, resolver), on_batch,
                  f"search_{slugify(query)}", max_scrolls, merge=_merge_hit,
                  collect=_collect_roots_ordered)


def _expand(page, responses, cfg, post, resolver):
    """Open one post's permalink: the post page renders its top comments,
    which search results leave out."""
    posts, stats = _crawl(page, post["permalink"], cfg, responses,
                          lambda root: _story_to_hit(root, resolver),
                          lambda *_: True, f"expand_{post['id']}", 1, merge=_merge_hit)
    return posts.get(post["id"]), stats


def search(cfg: Config, queries: list[str], filters: Filters = Filters(),
           max_results: int = 100, expand: int = 0, refresh: bool = False,
           session=None, now: datetime | None = None) -> tuple[int, Path | None, dict]:
    """Run `queries`, store hits, write a run directory. Returns
    (exit code, run dir or None, run record). `session` = (page, responses)
    replaces the real browser (tests)."""
    now = now or datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    day = now.date().isoformat()
    fkey = filters.canonical()
    resolver = OriginResolver(cfg)
    con = db.connect(cfg.db_path)
    record = {"created_at": now_iso, "queries": [], "filters": json.loads(fkey),
              "max_results": max_results, "expand": {"requested": expand, "done": 0},
              "posts": [], "error": None}
    try:
        plan = []
        for q in queries:
            qid = db.query_id(con, q, fkey, now_iso)
            last = db.query_last_run(con, qid)
            fresh = last and datetime.fromisoformat(last) > now - timedelta(hours=CACHE_HOURS)
            plan.append((q, qid, "cache" if fresh and not refresh else "live"))
        con.commit()

        cap = cfg.search_budget
        used = db.budget_used(con, day)
        live = [p for p in plan if p[2] == "live"]
        need = {"page_loads": len(live) + expand, "queries": len(live)}
        short = [f"{k} {used[k]}+{n} > {cap[k]}" for k, n in need.items() if used[k] + n > cap[k]]
        if live and used["scrolls"] >= cap["scrolls"]:
            short.append(f"scrolls {used['scrolls']} >= {cap['scrolls']}")
        if short:
            raise BudgetError("daily search budget would be exceeded: " + "; ".join(short)
                              + " (config search_budget; resets at midnight)")

        run_posts: dict[str, dict] = {}

        def add(row: dict, q: str, rank: int):
            p = run_posts.setdefault(row["id"], {**row, "matched": []})
            p["matched"].append({"query": q, "rank": rank})

        with ExitStack() as stack:
            if session:
                page, responses = session
            elif live or expand:
                page, responses = stack.enter_context(_browser(cfg))
            else:
                page = responses = None
            for q, qid, source in plan:
                entry = {"text": q, "source": source, "hits": 0, "scrolls": 0}
                record["queries"].append(entry)
                if source == "live":
                    left = cap["scrolls"] - db.budget_used(con, day)["scrolls"]
                    db.budget_add(con, day, page_loads=1, queries=1)
                    con.commit()
                    print(f"Searching {q!r} ({filters.describe()}) …")
                    posts, stats = _crawl_search(page, responses, cfg, q, filters, resolver,
                                                 max_results, max(0, min(cfg.max_scrolls, left)))
                    db.budget_add(con, day, scrolls=stats["scrolls"])
                    entry["scrolls"] = stats["scrolls"]
                    ranked = [p for p in posts.values() if filters.keep(p["posted_at"])][:max_results]
                    for p in ranked:
                        p["scraped_at"] = now_iso
                        db.upsert_post(con, p)
                        o = p["origin"]
                        if o["kind"] != "unknown":
                            db.upsert_source(con, o["slug"], o["kind"], o["name"], o["url"])
                    db.record_hits(con, qid, [p["id"] for p in ranked], now_iso)
                    con.commit()
                for row in db.hits_for(con, qid):
                    add(dict(row), q, row["rank"])
                entry["hits"] = sum(1 for p in run_posts.values()
                                    if any(m["query"] == q for m in p["matched"]))

            if expand:
                todo = sorted((p for p in run_posts.values()
                               if (p["comment_count"] or 0) > 0 and "[top comments]" not in p["text"]),
                              key=lambda p: -(p["comment_count"] or 0))[:expand]
                for p in todo:
                    db.budget_add(con, day, page_loads=1)
                    got, stats = _expand(page, responses, cfg, p, resolver)
                    db.budget_add(con, day, scrolls=stats["scrolls"])
                    if got and len(got["text"]) > len(p["text"]):
                        got.update(scraped_at=now_iso, group_slug=p["group_slug"],
                                   permalink=p["permalink"])
                        db.upsert_post(con, got)
                        p["text"] = got["text"]
                        record["expand"]["done"] += 1
                    con.commit()
    except BudgetError as e:
        record["error"] = str(e)
        con.close()
        print(f"error: {e}")
        return EXIT_BUDGET, None, record
    except CheckpointError as e:
        record["error"] = str(e)
        print(f"error: {e}")
        code = EXIT_CHECKPOINT
    else:
        code = EXIT_OK if run_posts else EXIT_NO_HITS

    con.commit()
    record["budget"] = {"cap": cfg.search_budget, "used_today": db.budget_used(con, day)}
    con.close()

    ordered = sorted(run_posts.values(),
                     key=lambda p: (min(m["rank"] for m in p["matched"]), -len(p["matched"])))
    record["posts"] = [{
        "id": p["id"],
        "matched": p["matched"],
        "origin": {"slug": p["group_slug"], "kind": p.get("source_kind") or "unknown",
                   "name": p.get("source_name"), "url": p.get("source_url")},
        "author": p["author"], "posted_at": p["posted_at"], "permalink": p["permalink"],
        "comment_count": p["comment_count"], "has_comments": "[top comments]" in (p["text"] or ""),
        "attachment": p["attachment"], "chars": len(p["text"] or ""),
    } for p in ordered]

    run_dir = _write_run(cfg, queries, now, record, ordered, filters)
    return code, run_dir, record


def _write_run(cfg, queries, now, record, ordered, filters) -> Path:
    slug = slugify(queries[0]) + (f"+{len(queries) - 1}-more" if len(queries) > 1 else "")
    run_dir = cfg.runs_dir / f"{slug}-{now:%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))

    meta = {p["id"]: p for p in record["posts"]}
    lines = [f"# fbtool search: {' | '.join(queries)}",
             f"{len(ordered)} posts · filters: {filters.describe()} · {record['created_at']}",
             "Blocks are ordered by best search rank. `matched` = query and rank; "
             "comments are present only when the post was expanded.", ""]
    for p in ordered:
        m = meta[p["id"]]
        o = m["origin"]
        label = {"author": "page/profile"}.get(o["kind"], o["kind"])
        where = f"{label}: {o['name'] or o['slug']}" + (f" ({o['url']})" if o["url"] else "")
        matched = ", ".join(f'"{x["query"]}" #{x["rank"]}' for x in p["matched"])
        cc = p["comment_count"]
        lines.append(f"--- post {p['id']} | {where} | author: {p['author'] or 'unknown'} | "
                     f"time: {p['posted_at'] or 'unknown'} | link: {p['permalink']} | "
                     f"matched: {matched} | comments: {cc if cc is not None else '?'}")
        lines.append(p["text"] or "")
        if p["attachment"]:
            lines.append(f"[attachment] {p['attachment']}")
        lines.append("")
    (run_dir / "corpus.md").write_text("\n".join(lines))
    return run_dir
