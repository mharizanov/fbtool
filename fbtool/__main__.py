import argparse
import sys
from datetime import datetime

from . import db
from .config import load


def cmd_login(cfg, args):
    from . import scrape
    scrape.login(cfg)


def cmd_scrape(cfg, args):
    from . import scrape
    from .delta import select_groups
    if not cfg.groups:
        sys.exit("No groups configured — edit config.yaml first.")
    needle = getattr(args, "group", None)
    since_arg = getattr(args, "since", None)
    groups = select_groups(cfg, needle)
    since = datetime.fromisoformat(since_arg) if since_arg else None
    # Captured before scraping so every post stored below has scraped_at >=
    # started — the boundary `fbtool delta` compares against.
    started = datetime.now().isoformat(timespec="seconds")
    posts = scrape.scrape_all(cfg, groups=groups, since=since)
    con = db.connect(cfg.db_path)
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    with con:
        for post in posts:
            counts[db.upsert_post(con, post, owner=True)] += 1
        # A targeted backfill isn't a "run" in the delta sense: recording it
        # would make the next incremental scrape skip what the other feeds
        # posted meanwhile, and would pollute `delta` baselines.
        if not needle and not since_arg:
            db.record_run(con, started)
    con.close()
    print(f"Stored {len(posts)} posts in {cfg.db_path} "
          f"({counts['new']} new, {counts['updated']} updated, "
          f"{counts['unchanged']} unchanged)")


def cmd_delta(cfg, args):
    from . import delta
    if args.ai:
        delta.ai_digest(cfg, group=args.group, runs_back=args.runs, since=args.since)
    else:
        delta.report(cfg, group=args.group, runs_back=args.runs,
                     since=args.since, full=args.full)


def cmd_groups(cfg, args):
    from . import groups
    groups.report(cfg, as_yaml=args.yaml)


def cmd_search(cfg, args):
    from datetime import date
    from . import search
    filters = search.Filters(
        recent=args.recent,
        since=date.fromisoformat(args.since) if args.since else None,
        until=date.fromisoformat(args.until) if args.until else None)
    code, run_dir, record = search.search(cfg, args.queries, filters, max_results=args.max,
                                          expand=args.expand, refresh=args.refresh)
    if run_dir:
        used, cap = record["budget"]["used_today"], record["budget"]["cap"]
        print(f"{len(record['posts'])} posts from {len(args.queries)} quer"
              f"{'y' if len(args.queries) == 1 else 'ies'} -> {run_dir / 'corpus.md'} "
              f"(today: {used['page_loads']}/{cap['page_loads']} loads, "
              f"{used['scrolls']}/{cap['scrolls']} scrolls, {used['queries']}/{cap['queries']} queries)")
    sys.exit(code)


def cmd_find(cfg, args):
    from . import find
    code, found = find.find(cfg, args.kind, " ".join(args.query), max_results=args.max)
    find.report(cfg, args.kind, found, as_yaml=args.yaml)
    sys.exit(code)


def cmd_summarize(cfg, args):
    from . import summarize
    out = summarize.summarize(cfg, days=args.days)
    print(f"Summary written to {out}")


def cmd_run(cfg, args):
    cmd_scrape(cfg, args)
    cmd_summarize(cfg, args)


def main():
    parser = argparse.ArgumentParser(prog="fbtool",
                                     description="Monitor, summarize and search Facebook groups and Pages")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="open a browser to log in to Facebook (one-time)")
    p_scrape = sub.add_parser("scrape", help="scrape configured groups into the database")
    p_scrape.add_argument("--group",
                          help="only groups whose slug or name contains this")
    p_scrape.add_argument("--since",
                          help="backfill: scrape back to this date (YYYY-MM-DD) instead "
                               "of the incremental cutoff; not recorded as a run")

    p_groups = sub.add_parser("groups",
                              help="list the groups the logged-in account is a member of")
    p_groups.add_argument("--yaml", action="store_true",
                          help="print config.yaml entries for groups not yet monitored")

    p_search = sub.add_parser(
        "search", help="search public posts by keyword; writes runs/<slug>/corpus.md + run.json",
        description="Exit codes: 0 hits, 1 no hits, 2 daily budget refused, 3 login/checkpoint.")
    p_search.add_argument("queries", nargs="+", help="one or more search queries")
    p_search.add_argument("--recent", action="store_true", help="Facebook's 'Recent posts' filter")
    p_search.add_argument("--since", help="posts on/after YYYY-MM-DD")
    p_search.add_argument("--until", help="posts on/before YYYY-MM-DD")
    p_search.add_argument("--max", type=int, default=100, help="max posts per query (default 100)")
    p_search.add_argument("--expand", type=int, default=0, metavar="N",
                          help="open the N most-commented hits to collect their top comments "
                               "(one page load each)")
    p_search.add_argument("--refresh", action="store_true",
                          help="ignore results cached in the last 24 h")

    p_find = sub.add_parser(
        "find", help="discover groups or Pages by topic; --yaml prints config entries",
        description="Counts against the daily search budget (one page load). "
                    "Exit codes: 0 found, 1 none, 2 budget refused, 3 login/checkpoint.")
    p_find.add_argument("kind", choices=["groups", "pages"])
    p_find.add_argument("query", nargs="+")
    p_find.add_argument("--max", type=int, default=20, help="max results (default 20)")
    p_find.add_argument("--yaml", action="store_true",
                        help="print config.yaml entries for results not yet monitored")

    p_sum = sub.add_parser("summarize", help="summarize stored posts with the configured AI model")
    p_sum.add_argument("--days", type=int, default=None,
                       help="days back to summarize (default: days_back from config)")

    p_run = sub.add_parser("run", help="scrape then summarize")
    p_run.add_argument("--days", type=int, default=None)

    p_delta = sub.add_parser("delta",
                             help="show what's new since a previous scrape run")
    p_delta.add_argument("--group",
                         help="only groups whose slug or name contains this")
    p_delta.add_argument("--runs", type=int, default=1,
                         help="scrape runs back to compare against "
                              "(default 1: what the latest scrape brought in)")
    p_delta.add_argument("--since",
                         help="explicit ISO timestamp baseline (overrides --runs)")
    p_delta.add_argument("--full", action="store_true",
                         help="print full text instead of snippets")
    p_delta.add_argument("--ai", action="store_true",
                         help="brief with the configured model instead of listing")

    args = parser.parse_args()
    cfg = load()
    {"login": cmd_login, "scrape": cmd_scrape, "summarize": cmd_summarize,
     "run": cmd_run, "delta": cmd_delta, "groups": cmd_groups,
     "search": cmd_search, "find": cmd_find}[args.command](cfg, args)


if __name__ == "__main__":
    main()
