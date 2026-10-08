"""Discover groups and Pages by topic (facebook.com/search/groups|pages).

Each result arrives as an edge with role ENTITY_GROUPS / ENTITY_PAGES; the
entity itself sits in rendering_strategy.view_model.profile, next to a
summary line ("Public · 30K members · 10+ posts a day", or a Page's
category and followers) and, for groups, the viewer's join state. Matched
on those field names, not on the full JSON path. Counts against the search
budget like `fbtool search`.
"""

import json
import re
from contextlib import ExitStack
from datetime import datetime
from urllib.parse import quote

from . import db
from .config import Config
from .scrape import CheckpointError, _check_session, _dig, _iter_json_docs, _pause, _walk
from .search import EXIT_BUDGET, EXIT_CHECKPOINT, EXIT_NO_HITS, EXIT_OK, BudgetError, _browser

FIND_URL = "https://www.facebook.com/search/{kind}?q="
ROLES = {"groups": "ENTITY_GROUPS", "pages": "ENTITY_PAGES"}
GROUP_SLUG_RE = re.compile(r"facebook\.com/groups/([^/?#]+)")
PAGE_SLUG_RE = re.compile(r"facebook\.com/(?:profile\.php\?id=(\d+)|([^/?#]+))")
STALE_SCROLL_LIMIT = 2


def ingest(bodies: list[str], kind: str, seen: dict) -> int:
    """Merge every entity result in `bodies` into `seen` (keyed by id).
    Returns how many were new."""
    role, new = ROLES[kind], 0
    for body in bodies:
        for doc in _iter_json_docs(body):
            for d in _walk(doc):
                edges = d.get("edges")
                if not isinstance(edges, list):
                    continue
                for e in edges:
                    if not isinstance(e, dict) or (e.get("node") or {}).get("role") != role:
                        continue
                    vm = _dig(e, "view_model", dict) or {}
                    prof = vm.get("profile") or {}
                    eid = prof.get("id")
                    if not isinstance(eid, str) or eid in seen:
                        continue
                    url = prof.get("url") or prof.get("profile_url") or ""
                    if kind == "groups":
                        m = GROUP_SLUG_RE.search(url)
                        slug = m.group(1) if m else eid
                    else:
                        m = PAGE_SLUG_RE.search(url)
                        slug = (m.group(1) or m.group(2)) if m else eid
                    summary = (vm.get("primary_snippet_text_with_entities") or {}).get("text")
                    desc = " ".join(x["text"] for x in vm.get("description_snippets_text_with_entities") or []
                                    if isinstance(x, dict) and isinstance(x.get("text"), str))
                    join = next((x["viewer_join_state"] for x in _walk(e)
                                 if isinstance(x.get("viewer_join_state"), str)), None)
                    privacy = next((p for p in ("Public", "Private") if (summary or "").startswith(p)), None)
                    seen[eid] = {"id": eid, "slug": slug, "name": prof.get("name"), "url": url or None,
                                 "summary": summary, "description": desc or None,
                                 "privacy": privacy, "join_state": join,
                                 # a Page without a vanity name goes by its numeric id;
                                 # facebook.com/<id> serves its feed (verified 2026-10-08)
                                 "vanity": not (kind == "pages" and slug.isdigit())}
                    new += 1
    return new


def find(cfg: Config, kind: str, query: str, max_results: int = 20,
         session=None, now: datetime | None = None) -> tuple[int, list[dict]]:
    now = now or datetime.now()
    day = now.date().isoformat()
    con = db.connect(cfg.db_path)
    seen: dict[str, dict] = {}
    try:
        cap, used = cfg.search_budget, db.budget_used(con, day)
        short = [f"{k} {used[k]}+1 > {cap[k]}" for k in ("page_loads", "queries") if used[k] + 1 > cap[k]]
        if short:
            raise BudgetError("daily search budget would be exceeded: " + "; ".join(short)
                              + " (config search_budget; resets at midnight)")
        db.budget_add(con, day, page_loads=1, queries=1)
        con.commit()
        with ExitStack() as stack:
            page, responses = session or stack.enter_context(_browser(cfg))
            responses.clear()
            page.goto(FIND_URL.format(kind=kind) + quote(query), wait_until="domcontentloaded",
                      timeout=60_000)
            page.wait_for_timeout(6000)
            _check_session(page, at_load=True)

            def drain():
                out = []
                while responses:
                    try:
                        out.append(responses.pop(0).text())
                    except Exception:
                        continue
                return out

            ingest(page.evaluate("""[...document.querySelectorAll('script[type="application/json"]')]
                                        .map(s => s.textContent)""") + drain(), kind, seen)
            stale = scrolls = 0
            while len(seen) < max_results and stale < STALE_SCROLL_LIMIT and scrolls < cfg.max_scrolls:
                scrolls += 1
                page.mouse.wheel(0, 6000)
                page.evaluate("window.scrollBy(0, 6000)")
                page.wait_for_timeout(_pause(cfg))
                _check_session(page, at_load=False)
                stale = 0 if ingest(drain(), kind, seen) else stale + 1
            db.budget_add(con, day, scrolls=scrolls)
        found = list(seen.values())[:max_results]
        for f in found:
            db.upsert_source(con, f["slug"] if kind == "pages" else f["id"],
                             "group" if kind == "groups" else "page", f["name"], f["url"])
        con.commit()
        return (EXIT_OK if found else EXIT_NO_HITS), found
    except BudgetError as e:
        print(f"error: {e}")
        return EXIT_BUDGET, []
    except CheckpointError as e:
        print(f"error: {e}")
        return EXIT_CHECKPOINT, list(seen.values())
    finally:
        con.commit()
        con.close()


def report(cfg: Config, kind: str, found: list[dict], as_yaml: bool = False) -> None:
    configured = {g.slug.lower() for g in cfg.groups}
    is_conf = lambda f: f["slug"].lower() in configured or f["id"] in configured
    if as_yaml:
        todo = [f for f in found if not is_conf(f)]
        for f in todo:
            entry = (f"  - slug: {json.dumps(f['slug'])}\n"
                     f"    name: {json.dumps(f['name'] or f['slug'], ensure_ascii=False)}")
            if kind == "pages":
                entry += "\n    type: page"
            note = None
            if kind == "groups" and f["join_state"] == "CAN_REQUEST":
                note = "join required (membership needs approval)"
            elif kind == "groups" and f["privacy"] == "Private" and f["join_state"] != "MEMBER":
                note = "private group: join it first"
            if note:
                entry = f"  # {note}\n" + "\n".join("  # " + line.strip() for line in entry.splitlines())
            print(entry)
        if not todo:
            print("# every result is already in config.yaml")
        return
    width = max((len(f["slug"]) for f in found), default=0)
    for f in found:
        state = (f["join_state"] or "-") if kind == "groups" else "page"
        print(f"{'*' if is_conf(f) else ' '} {f['slug']:<{width}}  {state:<11}  {f['name']}"
              f"{'  · ' + f['summary'] if f['summary'] else ''}")
    print(f"\n{len(found)} {kind} found, {sum(map(is_conf, found))} monitored (*)")
