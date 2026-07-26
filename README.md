# fbtool — Facebook group monitor & summarizer

Turns Facebook groups you're a member of — including private ones — into a
periodic LLM digest, entirely on your own machine. It scrapes with a real
(logged-in) browser, stores posts and their top comments in SQLite, and
summarizes the last *n* days of discussion (OpenAI by default; Anthropic,
or a local model via Ollama/vLLM, optional).

This fills a gap existing tools don't: scraping APIs stop at raw data and
want your session cookies on their servers, keyword-alert services monitor
with their own accounts so they can't get into members-only groups, and
the classic DOM scrapers break whenever Facebook reshuffles its markup.
fbtool captures the feed's GraphQL payloads instead, and your session
never leaves your machine.

Facebook has no API for groups, so collection is browser automation against
a logged-in account. This is against Facebook's ToS — read
[Account risk](#account-risk) before pointing it at an account you care
about.

## How it works

Facebook's rendered DOM is hostile to scraping: timestamps are
character-shuffled spans, permalinks materialize lazily, and class names are
generated. So fbtool doesn't parse the DOM at all. Instead:

1. Playwright launches Chromium with a **persistent profile** (`profile/`),
   so your Facebook login survives between runs.
2. It opens the group feed sorted chronologically and scrolls, exactly like
   a human reading the page.
3. While the page loads more posts, fbtool captures the `/api/graphql/`
   responses the feed itself fetches (plus the JSON embedded in the initial
   HTML). These payloads carry exact creation times, full post text,
   authors, permalinks, and top comments — everything the DOM obfuscates.
4. Extraction is structural, not path-based: it looks for story nodes
   (dicts carrying `comet_sections`) and pairs each `post_id` with its
   `creation_time`, which Facebook keeps in *sibling* JSON branches, via a
   minimal-subtree search. Partial renderings of the same post are merged;
   up to 8 top comments are kept per post.
5. Scrolling stops once loaded batches fall past the `days_back` window, or
   after several scrolls that surface nothing new. Posts are upserted into
   SQLite, and the summarizer sends the window's corpus to the model.

## Why it may fail

- **Payload structure changes** — the main failure mode. The extractor
  keys on field names (`post_id`, `creation_time`, `comet_sections`) rather
  than exact JSON paths, which survives most reshuffles, but Facebook can
  rename fields outright. Each run prints a diagnostics line (scrolls /
  payloads / story nodes / unique posts); if a run suddenly yields 0 posts,
  rerun with `FBTOOL_DUMP=<dir>` to save every captured payload and inspect
  what the structure looks like now.
- **Session expiry or a checkpoint** — scraping reports "Not logged in".
  Rerun `python -m fbtool login` and complete whatever Facebook asks.
- **Bot detection** — headless mode, a datacenter/VPN IP, or an IP in a new
  country are the usual triggers. Keep `headless: false` and run from the
  network you normally use Facebook on.
- **Overlays** — cookie-consent or login-nag dialogs can block the feed
  from loading. Run headed and watch what the page does.

## Account risk

Automated collection violates Facebook's ToS, and the enforcement is real
but graduated: first a **checkpoint** (captcha, SMS or photo verification),
then temporary feature blocks, and — for repeated or aggressive automation —
a permanent disable. Gentle use (one headed run per day, a couple of
groups, a residential IP the account normally uses) sits at the low end of
that curve, but the risk is never zero.

Practical guidance:

- Don't run this on an account you cannot afford to lose.
- A **fake/disposable account is not the answer**: new accounts have no
  trust and trip detection far faster, fake names violate Facebook's
  real-name policy and get disabled on their own, and a private group has
  to admit the account anyway.
- The reasonable middle ground is a **legitimate secondary account that is
  a real member of the group**, so a worst-case ban doesn't take your
  primary account with it.
- Keep the cadence low, keep `headless: false`, and don't suddenly run the
  scrape from a VPS in another country with the same profile.

## Setup

```sh
cd fbtool
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium
```

1. Copy the config and add your group slugs (the part after
   `facebook.com/groups/`):

   ```sh
   cp config.example.yaml config.yaml
   ```

2. Configure summarization in `config.yaml`. The default is
   `provider: openai` — set `openai_api_key` there, or leave it unset and
   export `OPENAI_API_KEY`. For Claude, set `provider: anthropic`, a
   `model` like `claude-opus-4-8`, and export `ANTHROPIC_API_KEY`.

   For a local model, no key is needed: set `provider: ollama` (default
   endpoint `http://localhost:11434/v1`) or `provider: vllm`
   (`http://localhost:8000/v1`), point `model` at a model you're serving,
   and use `base_url` if the server runs elsewhere. Note the summarizer
   sends all posts in one prompt — with a couple of active groups that can
   be tens of thousands of tokens, so serve the model with a context window
   to match (e.g. `OLLAMA_CONTEXT_LENGTH=32768`; Ollama's default is much
   smaller and will silently truncate).

3. Log in once (session persists in `profile/`):

   ```sh
   venv/bin/python -m fbtool login
   ```

4. Run:

   ```sh
   venv/bin/python -m fbtool run          # scrape + summarize
   ```

Summaries land in `summaries/YYYY-MM-DD.md`.

Individual steps: `fbtool scrape`, `fbtool summarize [--days N]`.

## Using the scraped data for your own LLM analysis

The database is the real product; the daily summary is just one consumer of
it. Everything lands in a single `posts` table in `fbtool.db`:

- `id` — Facebook post id (posts are upserted, so re-scrapes update a
  post's text as comments accumulate)
- `group_slug`, `author`, `permalink`
- `text` — the post body, followed by up to 8 top comments under a
  `[top comments]` marker
- `posted_at` — exact ISO timestamp (null when it couldn't be paired)
- `scraped_at` — when the row was last written

Dump a window with plain SQL:

```sh
sqlite3 fbtool.db \
  "select posted_at, author, text from posts
   where posted_at > datetime('now', '-30 days') order by posted_at"
```

Or, from Python, get the same prompt-ready corpus the summarizer uses —
one block per group, each post prefixed with its id, author, time, and
permalink:

```python
from datetime import datetime, timedelta
from fbtool.config import load
from fbtool.summarize import build_corpus

corpus, n_posts = build_corpus(load(), datetime.now() - timedelta(days=30))
```

Pair that corpus with your own instructions instead of the built-in
summary prompt. Things that work well on group data like this:

- **Q&A**: "what has been said about the garage door problem, with links?"
- **Action items**: extract deadlines, votes, and announcements into a list.
- **Trends over time**: run the same question over month-sized windows and
  compare — recurring complaints, sentiment shifts, who answers questions.
- **Structured extraction**: pull recommendations (services, phone numbers,
  prices) into JSON for a searchable list.

Posts average a few hundred tokens, so a quiet group's 60-day window fits
in any model's context; for busy groups, chunk by week or month and
aggregate the per-chunk results.

Two working examples live in `examples/`, both built on `build_corpus()` /
`complete()` from the backend:

- **`ask.py`** — one-shot Q&A over the history:
  `venv/bin/python examples/ask.py "who recommended a tailor?"`
- **`wishlist_watch.py`** — daily buy/sell watch: put plain-language wishes
  in `examples/wishlist.yaml` (copy the `.example`), run it after each
  scrape, and it prints newly-listed matches with permalinks. Scanned post
  ids are remembered in a state file, and the exit code is 0 only on a
  match, so a cron/launchd line can chain a notification.

## Scheduling (optional)

fbtool is a manual CLI — run `fbtool run` whenever you want a fresh digest.
If you'd rather have it on a schedule, any scheduler that can run a command
in the project directory works. Keep the cadence gentle — once a day is
plenty (see [Account risk](#account-risk)).

**cron:**

```
0 8 * * * cd /path/to/fbtool && venv/bin/python -m fbtool run >> logs/fbtool.log 2>&1
```

**launchd (macOS):** a template is in `launchd/com.fbtool.plist` — edit the
schedule and replace `/path/to/fbtool` with your checkout's path, then:

```sh
mkdir -p logs
cp launchd/com.fbtool.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fbtool.plist
```

Either way, with `headless: false` a Chromium window briefly opens during
each run — that's deliberate (headless browsers trip Facebook's bot
detection much more often), so schedule it on a machine with a display
session.

## Maintenance notes

- Extraction lives in `fbtool/scrape.py` (`_story_to_post`, `_find_units`);
  see [Why it may fail](#why-it-may-fail) for the debugging workflow.
- Timestamps come from exact `creation_time` values in the GraphQL payloads;
  posts whose timestamp can't be paired up are still kept (with a null
  `posted_at`).
