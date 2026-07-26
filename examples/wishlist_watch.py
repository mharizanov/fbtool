"""Watch buy/sell posts for wishlist matches (example #2: marketplace watch).

Reads a plain-language wishlist (a YAML list of strings), scans recent posts
straight from the fbtool database, and asks the configured model which posts
offer something on the list. Post ids that were already scanned are remembered
in a state file next to the database, so a daily run (e.g. right after
`fbtool scrape`) only looks at new posts.

Usage, from the project root:

    cp examples/wishlist.example.yaml examples/wishlist.yaml   # then edit
    venv/bin/python examples/wishlist_watch.py [--days 2] [--wishlist PATH]

Exits 0 with matches printed, 1 when there is nothing new — so a cron line
can chain a notification:

    ... wishlist_watch.py && osascript -e 'display notification "wishlist hit"'
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from fbtool import db
from fbtool.config import load
from fbtool.summarize import complete

SYSTEM = """You match Facebook group posts against a wishlist of things the
user wants to buy or rent. Only count posts OFFERING something (for sale, for
rent, given away) — not posts looking to buy. Reply with a JSON array only,
no prose and no code fences; one element per matching post:
{"item": "<wishlist entry>", "post_id": "...", "permalink": "...",
 "price": "<as stated, or null>", "reason": "<one line>"}
An empty array means nothing matched."""


def main():
    ap = argparse.ArgumentParser(description="Alert on wishlist matches in recent posts")
    ap.add_argument("--days", type=int, default=2,
                    help="how far back to scan (default 2 — meant to run daily)")
    ap.add_argument("--wishlist", type=Path,
                    default=Path(__file__).parent / "wishlist.yaml")
    args = ap.parse_args()

    wants = yaml.safe_load(args.wishlist.read_text())
    if not wants:
        sys.exit(f"{args.wishlist} is empty — add some wishes first.")

    cfg = load()
    since = (datetime.now() - timedelta(days=args.days)).isoformat(timespec="seconds")
    con = db.connect(cfg.db_path)
    rows = [r for g in cfg.groups for r in db.posts_since(con, g.slug, since)]
    con.close()

    state_path = cfg.db_path.with_name("wishlist_seen.json")
    seen = set(json.loads(state_path.read_text())) if state_path.exists() else set()
    rows = [r for r in rows if r["id"] not in seen]
    if not rows:
        print("No new posts to scan.")
        sys.exit(1)

    posts = "\n\n".join(f"--- post {r['id']} | link: {r['permalink']}\n{r['text']}"
                        for r in rows)
    prompt = ("Wishlist:\n" + "\n".join(f"- {w}" for w in wants)
              + f"\n\nPosts:\n\n{posts}")
    raw = complete(cfg, SYSTEM, prompt)

    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        sys.exit(f"Model did not return a JSON array:\n{raw}")
    matches = json.loads(raw[start:end + 1])

    # Remember everything scanned (not just matches) so tomorrow's run is cheap.
    state_path.write_text(json.dumps(sorted(seen | {r["id"] for r in rows})))

    if not matches:
        print(f"No matches in {len(rows)} new posts.")
        sys.exit(1)
    for m in matches:
        print(f"* {m['item']} — {m.get('price') or 'price n/a'} — {m.get('reason', '')}")
        print(f"  {m.get('permalink', '')}")


if __name__ == "__main__":
    main()
