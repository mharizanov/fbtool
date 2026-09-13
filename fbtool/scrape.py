"""Scrape Facebook group and Page feeds by capturing the GraphQL payloads the feed
itself loads, using a persistent logged-in browser profile.

Facebook obfuscates rendered timestamps (character-shuffled spans) and lazily
materializes permalinks, so DOM scraping is fragile. Instead we scroll the
feed and parse the /api/graphql/ responses the page fetches — those carry
exact creation times, full post text, authors, permalinks, and top comments.
"""

import json
import os
import re
from collections import deque
from pathlib import Path
from datetime import datetime, timedelta

from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import db
from .config import Config, Group

# Group posts: /groups/<slug>/posts/<numeric id>; Page posts:
# /<slug>/posts/<numeric id or pfbid…>.
POST_URL_RE = re.compile(
    r"facebook\.com/(?:groups/[^/?#]+/(?:posts|permalink)|[^/?#]+/posts)/[^/?#]+")

# Consecutive ingest batches whose oldest new post is older than the cutoff
# before we stop (the feed is creation-sorted, so 2 is already conservative).
PAST_WINDOW_LIMIT = 2
# Stop after this many consecutive scrolls that surface no new posts.
STALE_SCROLL_LIMIT = 6
# Stop after this many consecutive scroll batches whose posts are all
# already stored in the database — independent of the timestamp cutoff,
# and the fastest-triggering condition once the database is populated.
KNOWN_STREAK_LIMIT = 3


def _iter_json_docs(body: str):
    """GraphQL responses may contain several newline-separated JSON docs."""
    for line in body.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _walk(obj):
    """Yield every dict inside a nested JSON structure."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _dig(obj, key, want=None):
    """Breadth-first search for the first value of `key` (shallowest wins)."""
    q = deque([obj])
    while q:
        cur = q.popleft()
        if isinstance(cur, dict):
            if key in cur:
                v = cur[key]
                if want is None or isinstance(v, want):
                    return v
            q.extend(cur.values())
        elif isinstance(cur, list):
            q.extend(cur)
    return None


def _collect_roots(obj, out: list) -> None:
    """Collect every dict carrying a `comet_sections` key — Facebook's story
    renderings. Nested roots are collected too; the merge step dedupes."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "comet_sections" in cur:
                out.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _find_units(obj, out: list) -> tuple[bool, bool, bool]:
    """Append the minimal dicts whose subtree contains BOTH a post_id and a
    creation_time — Facebook keeps them in sibling branches, so this is how
    we pair a post with its timestamp. Returns (has_pid, has_ct, emitted)."""
    if isinstance(obj, dict):
        has_pid = isinstance(obj.get("post_id"), str)
        has_ct = isinstance(obj.get("creation_time"), int)
        emitted = False
        for v in obj.values():
            p, c, e = _find_units(v, out)
            has_pid |= p
            has_ct |= c
            emitted |= e
        if has_pid and has_ct and not emitted:
            out.append(obj)
            emitted = True
        return has_pid, has_ct, emitted
    if isinstance(obj, list):
        has_pid = has_ct = emitted = False
        for v in obj:
            p, c, e = _find_units(v, out)
            has_pid |= p
            has_ct |= c
            emitted |= e
        return has_pid, has_ct, emitted
    return False, False, False


def _valid_post_id(v) -> bool:
    return isinstance(v, str) and v.isdigit()


def _extract_comments(root: dict) -> list[str]:
    comments, seen_bodies = [], set()
    for d in _walk(root):
        c = d.get("comment")
        if not isinstance(c, dict):
            if d.get("__typename") == "Comment":
                c = d
            else:
                continue
        body = _dig(c, "body", dict) or {}
        ctext = body.get("text")
        if not ctext or ctext in seen_bodies:
            continue
        seen_bodies.add(ctext)
        cauthor = (_dig(c, "author", dict) or {}).get("name") or "?"
        comments.append(f"  > {cauthor}: {ctext[:500]}")
        if len(comments) >= 8:
            break
    return comments


def _story_to_post(story: dict, group: Group) -> dict | None:
    post_id = story.get("post_id")
    if not _valid_post_id(post_id):
        post_id = _dig(story, "post_id", str)
    if not _valid_post_id(post_id):
        return None

    ct = story.get("creation_time")
    if not isinstance(ct, int):
        ct = _dig(story, "creation_time", int)

    msg = _dig(story, "message", dict)
    text = (msg or {}).get("text") or ""

    author = None
    actors = _dig(story, "actors", list)
    if actors and isinstance(actors[0], dict):
        author = actors[0].get("name")

    # Prefer a permalink under this feed's own path: a shared post carries
    # the original's URL too, and _walk's order is not meaningful.
    url = None
    own_prefix = f"facebook.com/{group.path}/"
    for d in _walk(story):
        for key in ("wwwURL", "url", "permalink_url", "permalink"):
            v = d.get(key)
            if isinstance(v, str) and POST_URL_RE.search(v):
                v = v.split("?")[0]
                if own_prefix in v:
                    url = v
                    break
                url = url or v
        if url and own_prefix in url:
            break
    if url is None:
        url = group.post_url(post_id)

    comments = _extract_comments(story)
    if comments:
        text = (text + "\n[top comments]\n" + "\n".join(comments)).strip()
    if len(text) > 6000:
        text = text[:6000] + " […]"

    return {
        "id": post_id,
        "author": author,
        "text": text,
        "posted_at": datetime.fromtimestamp(ct).isoformat(timespec="seconds") if ct else None,
        "permalink": url,
        "group_slug": group.slug,
    }


def _merge(posts: dict, post: dict) -> bool:
    """Merge a (possibly partial) story rendering. Returns True if new."""
    old = posts.get(post["id"])
    if old is None:
        posts[post["id"]] = post
        return True
    if len(post["text"]) > len(old["text"]):
        old["text"] = post["text"]
    old["author"] = old["author"] or post["author"]
    old["posted_at"] = old["posted_at"] or post["posted_at"]
    return False


def scrape_group(page: Page, group: Group, cutoff: datetime, cfg: Config,
                 graphql_responses: list, known_ids: set[str] = frozenset()) -> list[dict]:
    graphql_responses.clear()
    page.goto(group.feed_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(6000)

    if page.query_selector('form[action*="login"]'):
        raise RuntimeError("Not logged in — run `python -m fbtool login` first.")

    posts: dict[str, dict] = {}
    stats = {"payloads": 0, "stories": 0, "scrolls": 0}

    # Set FBTOOL_DUMP=<dir> to save every ingested payload for offline
    # analysis of Facebook's (undocumented, shifting) response structure.
    dump_dir = os.environ.get("FBTOOL_DUMP")
    if dump_dir:
        Path(dump_dir).mkdir(parents=True, exist_ok=True)

    def ingest(bodies: list[str]) -> tuple[list[datetime], set[str]]:
        new_times = []
        touched_ids: set[str] = set()
        for body in bodies:
            stats["payloads"] += 1
            if dump_dir:
                (Path(dump_dir) / f"{group.slug}_{stats['payloads']:04d}.txt").write_text(body)
            for doc in _iter_json_docs(body):
                roots: list = []
                _collect_roots(doc, roots)
                for root in roots:
                    stats["stories"] += 1
                    post = _story_to_post(root, group)
                    if post:
                        touched_ids.add(post["id"])
                        if _merge(posts, post) and post["posted_at"]:
                            new_times.append(datetime.fromisoformat(post["posted_at"]))
                # Timestamp backfill: pair post_ids with creation_times that
                # live in sibling branches outside any single story root.
                units: list = []
                _find_units(doc, units)
                for unit in units:
                    pid = _dig(unit, "post_id", str)
                    ct = _dig(unit, "creation_time", int)
                    if _valid_post_id(pid) and ct and pid in posts and not posts[pid]["posted_at"]:
                        posts[pid]["posted_at"] = datetime.fromtimestamp(ct).isoformat(timespec="seconds")
        return new_times, touched_ids

    def drain() -> list[str]:
        bodies = []
        while graphql_responses:
            resp = graphql_responses.pop(0)
            try:
                bodies.append(resp.text())
            except Exception:
                continue
        return bodies

    # The first posts arrive as JSON embedded in the initial HTML, not XHR.
    embedded = page.evaluate(
        """[...document.querySelectorAll('script[type="application/json"]')]
               .map(s => s.textContent)"""
    )
    ingest(embedded)
    ingest(drain())

    past_batches = 0
    stale_scrolls = 0
    known_streak = 0
    for i in range(cfg.max_scrolls):
        stats["scrolls"] = i + 1
        page.mouse.wheel(0, 6000)
        page.evaluate("window.scrollBy(0, 6000)")
        page.wait_for_timeout(cfg.scroll_pause_ms)

        new_times, touched_ids = ingest(drain())

        # Independent of the timestamp cutoff below: once we're back into
        # territory the database already has, stop — this doesn't depend
        # on Facebook's ordering or timestamp parsing being exact.
        if touched_ids and touched_ids <= known_ids:
            known_streak += 1
            if known_streak >= KNOWN_STREAK_LIMIT:
                break
        else:
            known_streak = 0

        if new_times:
            stale_scrolls = 0
            if min(new_times) < cutoff:
                past_batches += 1
                if past_batches >= PAST_WINDOW_LIMIT:
                    break
            else:
                past_batches = 0
        else:
            stale_scrolls += 1
            if stale_scrolls >= STALE_SCROLL_LIMIT:
                break

    kept = [p for p in posts.values()
            if p["posted_at"] is None or datetime.fromisoformat(p["posted_at"]) >= cutoff]
    dated = [p["posted_at"] for p in posts.values() if p["posted_at"]]
    print(f"  diagnostics: {stats['scrolls']} scrolls, {stats['payloads']} payloads, "
          f"{stats['stories']} story nodes, {len(posts)} unique posts "
          f"(oldest {min(dated) if dated else 'n/a'}), {len(kept)} within window")
    return kept


def open_context(pw, cfg: Config, headless: bool | None = None) -> BrowserContext:
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(cfg.profile_dir),
        headless=cfg.headless if headless is None else headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _is_logged_in(ctx: BrowserContext) -> bool:
    """Facebook sets the `c_user` cookie only for an authenticated session."""
    return any(c.get("name") == "c_user" for c in ctx.cookies())


def login(cfg: Config) -> bool:
    """Open a headed browser so the user can log in manually. Polls for the
    session cookie and closes itself the moment login succeeds — no manual
    window close or scrolling needed. Returns whether login succeeded."""
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/")
        print("Log in to Facebook in the opened browser window.")
        print("It will close automatically once login is detected.")
        success = False
        try:
            while not _is_logged_in(ctx):
                page.wait_for_timeout(1000)
            success = True
        except KeyboardInterrupt:
            pass
        except Exception:
            pass  # window closed manually, or the page/context went away
        try:
            ctx.close()
        except Exception:
            pass
    print("Login detected — session saved." if success
          else "Login window closed without detecting a session.")
    return success


def ensure_logged_in(cfg: Config) -> None:
    """Check the persisted profile for a valid session without showing a
    browser window; only pop one open (via `login`) if a human needs to
    authenticate."""
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg, headless=True)
        logged_in = _is_logged_in(ctx)
        ctx.close()
    if logged_in:
        return
    print("No active Facebook session — opening a browser window to log in…")
    if not login(cfg):
        raise RuntimeError("Login did not complete — run `python -m fbtool login` first.")


def scrape_all(cfg: Config, groups: list[Group] | None = None,
               since: datetime | None = None) -> list[dict]:
    """Scrape `groups` (default: all configured) back to the usual
    incremental cutoff, or — for a backfill — back to `since`. A backfill
    ignores the already-known-posts early stop so it reaches past posts the
    database already holds."""
    ensure_logged_in(cfg)
    groups = cfg.groups if groups is None else groups

    days_cutoff = datetime.now() - timedelta(days=cfg.days_back)
    cutoff = days_cutoff
    known_ids_by_group: dict[str, set[str]] = {}
    con = db.connect(cfg.db_path)
    try:
        last_run = db.run_boundary(con, 1)
        if last_run:
            incremental_cutoff = datetime.fromisoformat(last_run) - timedelta(hours=cfg.overlap_hours)
            cutoff = max(days_cutoff, incremental_cutoff)
        if since is not None:
            cutoff = since
        for group in groups:
            known_ids_by_group[group.slug] = (
                frozenset() if since is not None else db.known_post_ids(con, group.slug))
    finally:
        con.close()

    scraped_at = datetime.now().isoformat(timespec="seconds")
    all_posts: list[dict] = []
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        graphql_responses: list = []
        page.on("response",
                lambda r: graphql_responses.append(r) if "/api/graphql" in r.url else None)

        for group in groups:
            print(f"Scraping {group.name} (facebook.com/{group.path}) …")
            posts = scrape_group(page, group, cutoff, cfg, graphql_responses,
                                 known_ids_by_group[group.slug])
            for post in posts:
                post["scraped_at"] = scraped_at
            print(f"  {len(posts)} posts since {cutoff:%Y-%m-%d}")
            all_posts.extend(posts)
        ctx.close()
    return all_posts
