"""Ask a question over the scraped group history (example #1: Q&A).

Uses the fbtool backend: build_corpus() for the prompt-ready post dump and
complete() for the configured AI provider.

Usage, from the project root:

    venv/bin/python examples/ask.py "what was said about the garage?"
    venv/bin/python examples/ask.py --days 180 "who recommended a tailor?"
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fbtool.config import load
from fbtool.summarize import build_corpus, complete

SYSTEM = """You answer questions about Facebook group discussions using only
the posts provided. Cite the posts you drew from — author, date, and
permalink — and quote short key phrases where it helps. If the posts don't
answer the question, say so plainly instead of guessing. Answer in the
language the question was asked in."""


def main():
    ap = argparse.ArgumentParser(description="Q&A over scraped group history")
    ap.add_argument("question")
    ap.add_argument("--days", type=int, default=None,
                    help="how far back to look (default: days_back from config)")
    args = ap.parse_args()

    cfg = load()
    days = args.days or cfg.days_back
    corpus, total = build_corpus(cfg, datetime.now() - timedelta(days=days))
    if total == 0:
        sys.exit("No posts in the database for that window — run a scrape first.")

    prompt = (f"Below are {total} posts from the last {days} days.\n\n{corpus}\n\n"
              f"Question: {args.question}")
    print(complete(cfg, SYSTEM, prompt))


if __name__ == "__main__":
    main()
