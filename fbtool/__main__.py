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
    if not cfg.groups:
        sys.exit("No groups configured — edit config.yaml first.")
    # Captured before scraping so every post stored below has scraped_at >=
    # started — the boundary `fbtool delta` compares against.
    started = datetime.now().isoformat(timespec="seconds")
    posts = scrape.scrape_all(cfg)
    con = db.connect(cfg.db_path)
    counts = {"new": 0, "updated": 0, "unchanged": 0}
    with con:
        for post in posts:
            counts[db.upsert_post(con, post)] += 1
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


def cmd_summarize(cfg, args):
    from . import summarize
    out = summarize.summarize(cfg, days=args.days)
    print(f"Summary written to {out}")


def cmd_run(cfg, args):
    cmd_scrape(cfg, args)
    cmd_summarize(cfg, args)


def main():
    parser = argparse.ArgumentParser(prog="fbtool",
                                     description="Monitor and summarize Facebook groups")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="open a browser to log in to Facebook (one-time)")
    sub.add_parser("scrape", help="scrape configured groups into the database")

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
     "run": cmd_run, "delta": cmd_delta}[args.command](cfg, args)


if __name__ == "__main__":
    main()
