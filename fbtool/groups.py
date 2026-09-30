"""List the groups the logged-in account is a member of.

Reads facebook.com/groups/joins/ ("Your groups") the same way the feed
scraper reads a feed: the list arrives as JSON embedded in the initial HTML,
and longer lists page in via /api/graphql/ while scrolling. Group nodes are
matched by `__typename == "Group"`, not by JSON path.
"""

import json
import re

from playwright.sync_api import sync_playwright

from .config import Config
from .scrape import _iter_json_docs, _walk, ensure_logged_in, open_context

JOINS_URL = "https://www.facebook.com/groups/joins/"
GROUP_URL_RE = re.compile(r"facebook\.com/groups/([^/?#]+)")
# Stop after this many consecutive scrolls that surface no new group.
STALE_SCROLL_LIMIT = 3


def _ingest(bodies: list[str], seen: dict[str, dict]) -> int:
    """Merge every Group node in `bodies` into `seen` (keyed by id). The
    same group is rendered several times (list, sidebar, pickers); a
    rendering carrying `viewer_join_state` is authoritative for membership.
    Returns how many ids were new."""
    new = 0
    for body in bodies:
        for doc in _iter_json_docs(body):
            for d in _walk(doc):
                gid = d.get("id")
                if d.get("__typename") != "Group" or not isinstance(gid, str):
                    continue
                g = seen.get(gid)
                if g is None:
                    g = seen[gid] = {"id": gid, "name": None, "slug": None, "join_state": None}
                    new += 1
                if not g["name"] and isinstance(d.get("name"), str):
                    g["name"] = d["name"]
                m = GROUP_URL_RE.search(d.get("url") or "")
                if m and not g["slug"]:
                    g["slug"] = m.group(1)
                if isinstance(d.get("viewer_join_state"), str):
                    g["join_state"] = d["viewer_join_state"]
    return new


def joined_groups(cfg: Config) -> list[dict]:
    """Return [{id, slug, name}] for the groups the account is a member of,
    sorted by name. `slug` is the vanity name when the group has one,
    otherwise the numeric id — either works as a config.yaml slug."""
    ensure_logged_in(cfg)
    seen: dict[str, dict] = {}
    with sync_playwright() as pw:
        ctx = open_context(pw, cfg)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        responses: list = []
        page.on("response",
                lambda r: responses.append(r) if "/api/graphql" in r.url else None)
        page.goto(JOINS_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6000)
        if page.query_selector('form[action*="login"]'):
            ctx.close()
            raise RuntimeError("Not logged in — run `python -m fbtool login` first.")

        def drain() -> list[str]:
            bodies = []
            while responses:
                try:
                    bodies.append(responses.pop(0).text())
                except Exception:
                    continue
            return bodies

        _ingest(page.evaluate(
            """[...document.querySelectorAll('script[type="application/json"]')]
                   .map(s => s.textContent)"""), seen)
        _ingest(drain(), seen)

        stale = 0
        for _ in range(cfg.max_scrolls):
            page.mouse.wheel(0, 6000)
            page.evaluate("window.scrollBy(0, 6000)")
            page.wait_for_timeout(cfg.scroll_pause_ms)
            if _ingest(drain(), seen):
                stale = 0
            else:
                stale += 1
                if stale >= STALE_SCROLL_LIMIT:
                    break
        ctx.close()

    # The page can also render groups the account isn't in (suggestions).
    # Keep explicit members; if no node carries a join state at all,
    # Facebook changed the payload — fall back to every group seen.
    states = [g["join_state"] for g in seen.values() if g["join_state"]]
    if states:
        groups = [g for g in seen.values() if g["join_state"] == "MEMBER"]
    else:
        print("warning: no viewer_join_state in payloads — listing every group "
              "seen on the page, which may include suggestions")
        groups = list(seen.values())
    return sorted(({"id": g["id"], "slug": g["slug"] or g["id"],
                    "name": g["name"] or g["id"]} for g in groups),
                  key=lambda g: g["name"].casefold())


def report(cfg: Config, as_yaml: bool = False) -> None:
    groups = joined_groups(cfg)
    configured = {g.slug for g in cfg.groups if g.kind == "group"}
    is_configured = lambda g: g["id"] in configured or g["slug"] in configured
    if as_yaml:
        # config.yaml entries for groups not yet monitored; JSON strings are
        # valid YAML scalars and handle quoting.
        todo = [g for g in groups if not is_configured(g)]
        for g in todo:
            print(f"  - slug: {json.dumps(g['slug'])}\n"
                  f"    name: {json.dumps(g['name'], ensure_ascii=False)}")
        if not todo:
            print("# every joined group is already in config.yaml")
        return
    width = max((len(g["slug"]) for g in groups), default=0)
    for g in groups:
        mark = "*" if is_configured(g) else " "
        print(f"{mark} {g['slug']:<{width}}  {g['name']}")
    n = sum(map(is_configured, groups))
    print(f"\n{len(groups)} groups joined, {n} monitored (*)")
