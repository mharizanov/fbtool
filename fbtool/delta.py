"""Answer "what's new in group X" from the database: posts first seen since a
baseline scrape run, plus older posts whose text grew (usually new comments).
Read-only over what upsert_post recorded — no scraping happens here."""

import sqlite3

from . import db
from .config import Config, Group

DELTA_SYSTEM = """You brief a reader on what is NEW in Facebook groups they
already follow, since they last checked. Cover new posts and the fresh
comments added to older posts. Entries marked [newly added] under an
[original post, for context] block are the fresh part — the original is
context only, don't re-summarize it. Group by topic, keep only what a group
member would actually want to know, include permalinks for the most
significant items, and skip pure noise. Write Markdown with one section per
group; say "nothing significant" for a group with no noteworthy news."""


def select_groups(cfg: Config, needle: str | None) -> list[Group]:
    if not needle:
        return cfg.groups
    n = needle.lower()
    matches = [g for g in cfg.groups if n in g.slug.lower() or n in g.name.lower()]
    if not matches:
        raise SystemExit(f"No configured group matches {needle!r} — "
                         f"have: {', '.join(g.name for g in cfg.groups)}")
    return matches


def _baseline(con: sqlite3.Connection, runs_back: int, since: str | None) -> str:
    if since:
        return since
    boundary = db.run_boundary(con, runs_back)
    if boundary is None:
        raise SystemExit(
            f"Fewer than {runs_back} scrape run(s) recorded (runs are recorded "
            f"since delta tracking was added) — pass --since ISO_TIMESTAMP instead.")
    return boundary


def _snippet(text: str, limit: int = 160) -> str:
    body = " ".join(text.split("\n[top comments]")[0].split())
    return body[:limit] + ("…" if len(body) > limit else "")


def _added_text(old: str | None, new: str) -> str | None:
    """The appended part when the update is a pure append (the common case:
    comments accumulating under an unchanged body); None for edits."""
    if old and new.startswith(old):
        return new[len(old):].strip()
    return None


def collect(con: sqlite3.Connection, groups: list[Group], since: str):
    """Per group: (group, new_rows, [(row, added_text_or_None, old_text)])."""
    out = []
    for g in groups:
        new = db.new_posts_since(con, g.slug, since)
        updated = []
        for r in db.updated_posts_since(con, g.slug, since):
            old = db.text_as_of(con, r["id"], since)
            updated.append((r, _added_text(old, r["text"]), old))
        out.append((g, new, updated))
    return out


def report(cfg: Config, group: str | None = None, runs_back: int = 1,
           since: str | None = None, full: bool = False) -> int:
    """Print the delta as a plain listing; returns new+updated post count."""
    con = db.connect(cfg.db_path)
    since = _baseline(con, runs_back, since)
    total = 0
    for g, new, updated in collect(con, select_groups(cfg, group), since):
        total += len(new) + len(updated)
        print(f"\n# {g.name} — {len(new)} new, {len(updated)} updated since {since}")
        for r in new:
            print(f"\n[new] {r['posted_at'] or '?'} — {r['author'] or 'unknown'}")
            print(f"  {r['permalink']}")
            print(f"  {r['text'] if full else _snippet(r['text'])}")
        for r, added, old in updated:
            print(f"\n[updated] {r['posted_at'] or '?'} — {r['author'] or 'unknown'}")
            print(f"  {r['permalink']}")
            print(f"  {_snippet(r['text'])}")
            if added:
                print(f"  added: {added if full else _snippet(added)}")
            else:
                print(f"  text changed (+{len(r['text'] or '') - len(old or '')} chars)")
    con.close()
    if total == 0:
        print("\nNothing new.")
    return total


def ai_digest(cfg: Config, group: str | None = None, runs_back: int = 1,
              since: str | None = None) -> None:
    """Send only the delta to the configured model and print its briefing."""
    from .summarize import complete

    con = db.connect(cfg.db_path)
    since = _baseline(con, runs_back, since)
    sections, total = [], 0
    for g, new, updated in collect(con, select_groups(cfg, group), since):
        total += len(new) + len(updated)
        lines = [f"## Group: {g.name} ({len(new)} new posts, {len(updated)} updated)"]
        for r in new:
            lines.append(
                f"\n--- new post {r['id']} | author: {r['author'] or 'unknown'} | "
                f"time: {r['posted_at'] or 'unknown'} | link: {r['permalink']}\n{r['text']}"
            )
        for r, added, old in updated:
            lines.append(
                f"\n--- update to post {r['id']} | author: {r['author'] or 'unknown'} | "
                f"time: {r['posted_at'] or 'unknown'} | link: {r['permalink']}"
            )
            if added:
                lines.append(f"[original post, for context]\n{_snippet(r['text'], 400)}")
                lines.append(f"[newly added]\n{added}")
            else:
                lines.append(f"[current text after an edit]\n{r['text']}")
        sections.append("\n".join(lines))
    con.close()

    if total == 0:
        print("Nothing new.")
        return
    prompt = (f"Below is everything new in these Facebook group(s) since {since} "
              f"({total} items). Brief me on it.\n\n" + "\n\n".join(sections))
    print(complete(cfg, DELTA_SYSTEM, prompt))
