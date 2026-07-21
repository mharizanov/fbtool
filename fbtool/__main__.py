import argparse
import sys

from . import db
from .config import load


def cmd_login(cfg, args):
    from . import scrape
    scrape.login(cfg)


def cmd_scrape(cfg, args):
    from . import scrape
    if not cfg.groups:
        sys.exit("No groups configured — edit config.yaml first.")
    posts = scrape.scrape_all(cfg)
    con = db.connect(cfg.db_path)
    with con:
        for post in posts:
            db.upsert_post(con, post)
    con.close()
    print(f"Stored/updated {len(posts)} posts in {cfg.db_path}")


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

    args = parser.parse_args()
    cfg = load()
    {"login": cmd_login, "scrape": cmd_scrape,
     "summarize": cmd_summarize, "run": cmd_run}[args.command](cfg, args)


if __name__ == "__main__":
    main()
