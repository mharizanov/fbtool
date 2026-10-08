# fbtool — Facebook group monitor, summarizer & search

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

Beyond the groups you follow, `fbtool search` turns Facebook's post search
into a research corpus: give it a few queries, and it writes every matching
public post (with its group or Page, author, time and link) to a Markdown
file an LLM can read. `fbtool find` discovers groups and Pages by topic.
See [Searching public posts](#searching-public-posts).

The backstory — why the groups are worth reading but the feed isn't — is in
the blog post: [Turning Facebook groups into a daily
digest](https://harizanov.com/2026/08/turning-facebook-groups-into-a-daily-digest/).

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
- **Bot detection** — a datacenter/VPN IP, an IP in a new country, or an
  unnaturally mechanical scroll/timing pattern are the usual triggers.
  Run from the network you normally use Facebook on; the `--disable-blink-
  features=AutomationControlled` launch flag and modern headless Chromium
  already remove most of the client-side automation signals.
- **Overlays** — cookie-consent or login-nag dialogs can block the feed
  from loading. Set `headless: false` in config.yaml temporarily and watch
  what the page does.

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
- Keep the cadence low, and don't suddenly run the scrape from a VPS in
  another country with the same profile.
- **Search is riskier than feed reading.** Facebook rate-limits search
  harder, so `search` and `find` run under a daily budget of page loads,
  scrolls and queries (`search_budget` in config.yaml, default 60 / 300 /
  20) and refuse up front when a run would exceed it. If Facebook shows a
  checkpoint or a login wall mid-run, every command stops at once instead
  of retrying.

## Setup

```sh
cd fbtool
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium
```

1. Copy the config and add your group slugs (the part after
   `facebook.com/groups/`). Public Pages work too: add the entry with
   `type: page` and the slug after `facebook.com/` (for a Page without a
   vanity name, the number from `profile.php?id=`). `fbtool find` prints
   ready-made entries.

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

Individual steps: `fbtool scrape`, `fbtool summarize [--days N]`,
`fbtool delta`.

To see which groups the logged-in account belongs to, run `fbtool groups`.
It reads facebook.com/groups/joins/ and marks the ones already in
`config.yaml` with `*`. With `--yaml` it prints config entries for the
groups that aren't monitored yet, ready to paste under `groups:`.

Scrapes are incremental: each run only goes back to the previous run (plus
`overlap_hours`). To backfill one feed further — e.g. a group or Page you
just added — give an explicit start date; such a run isn't recorded as a
baseline for `delta`:

```sh
venv/bin/python -m fbtool scrape --group mare --since 2025-08-01
```

## Searching public posts

`fbtool search` runs one or more queries through Facebook's post search in
a single browser session and writes a run directory:

```sh
venv/bin/python -m fbtool search "heat pump noise" "air-to-water heat pump review" \
    --since 2025-10-01 --max 40 --expand 5
# 112 posts from 2 queries -> runs/heat-pump-noise+1-more-20261008-133348/corpus.md
#   (today: 7/60 loads, 30/300 scrolls, 2/20 queries)
```

- `corpus.md` — one block per post: id, group or Page, author, time,
  permalink, which query found it at which rank, comment count, then the
  text (and a link attachment's title). Posts that contain a query word
  come first; below a marker line follow the loosely related results
  Facebook pads its lists with.
- `run.json` — the same, machine-readable, plus per-query status, cache use
  and the day's budget.

Options: `--recent` (Facebook's "Recent posts" filter), `--since` /
`--until YYYY-MM-DD` (Facebook's date filter, also applied to the results),
`--max N` posts per query (default 100), `--refresh` (ignore the 24-hour
result cache). Search results carry comment counts but not the comments;
`--expand N` opens the N most-commented hits to collect their top comments,
one page load each.

The tool does no LLM calls of its own: it is built to be driven by a person
or a coding agent (Claude Code, for one) that plans the queries, runs the
command, and reads `corpus.md`. Exit codes are stable for that: 0 hits, 1
no hits, 2 daily budget refused, 3 login wall or checkpoint.

Hits are stored in `fbtool.db` like feed posts. A hit from a group or Page
you monitor gets that feed's slug, so it is the same row the next scrape
updates; `delta` reports a post first found by a search only if it was also
posted after the baseline. Hits from anywhere else stay out of the daily
summary and `delta`. Run directories hold other people's posts and are
gitignored.

### Finding groups and Pages

```sh
venv/bin/python -m fbtool find groups "heat pumps"
venv/bin/python -m fbtool find pages "heat pump installer" --yaml
```

Lists results with the group's privacy, size and activity ("Public · 4.8K
members · 3 posts a day") and whether the account is a member, or a Page's
category and followers; monitored ones are marked `*`. `--yaml` prints
config entries for the rest. Public groups can be monitored without
joining; groups that need approval or are private come out commented. Each
`find` costs one page load of the search budget.

## What's new since the last scrape

Every scrape records a run, and `fbtool delta` reports what the latest one
brought in: posts seen for the first time, plus older posts whose text grew
(comments are folded into a post's text, so fresh comment activity on old
threads shows up here too).

```sh
venv/bin/python -m fbtool delta                   # since the previous scrape
venv/bin/python -m fbtool delta --group sailing   # one group, matched by name/slug
venv/bin/python -m fbtool delta --runs 3          # across the last three scrapes
venv/bin/python -m fbtool delta --since 2026-08-01T00:00:00
venv/bin/python -m fbtool delta --ai              # model briefing instead of a listing
```

`--full` prints whole posts instead of snippets. The superseded text of an
updated post is kept in `post_versions`, so the report shows just the newly
added part (or a size delta when a post was edited rather than appended to).

## Using the scraped data for your own LLM analysis

The database is the real product; the daily summary is just one consumer of
it. Everything lands in a single `posts` table in `fbtool.db`:

- `id` — Facebook post id (posts are upserted, so re-scrapes update a
  post's text as comments accumulate)
- `group_slug`, `author`, `permalink`
- `text` — the post body, followed by up to 8 top comments under a
  `[top comments]` marker
- `posted_at` — exact ISO timestamp (null when it couldn't be paired)
- `scraped_at` — when the post was first seen
- `updated_at` — when a re-scrape last grew the post's text (null: never)
- `attachment`, `comment_count` — a link attachment's title/description
  and the comment count, when the payload has them
- `found_by` — `search` when a search stored the post first (null: a feed
  scrape)

Two side tables support `fbtool delta`: `runs` (one row per scrape, the
baselines deltas compare against) and `post_versions` (the superseded text
of updated posts, so a delta can show exactly what was added). Search adds
`queries` and `search_hits` (which query returned which post at which
rank), `sources` (name and URL of the groups, Pages and authors hits come
from) and `search_budget` (per-day load).

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

Scraping itself runs headless, so no display session is required. A
Chromium window only opens automatically if the saved session has expired
and a human needs to log in — it closes itself the moment login is
detected. Run `python -m fbtool login` proactively before scheduling
unattended runs so that window never needs to appear on its own.

## Maintenance notes

- Extraction lives in `fbtool/scrape.py` (`_story_to_post`, `_find_units`);
  see [Why it may fail](#why-it-may-fail) for the debugging workflow.
- Timestamps come from exact `creation_time` values in the GraphQL payloads;
  posts whose timestamp can't be paired up are still kept (with a null
  `posted_at`).
- Delta tracking lives in `fbtool/db.py` (`upsert_post` decides
  new/updated/unchanged and archives superseded text) and `fbtool/delta.py`
  (read-only reporting). The scrape itself stays a full-window crawl on
  purpose — re-visiting known posts is what refreshes their comments; deltas
  are computed at the write path, not by crawling less.
- Search lives in `fbtool/search.py` (query URL and filters, origin
  resolution, budget, run directory) and `fbtool/find.py`; both reuse the
  feed's crawl loop (`_crawl` in `scrape.py`).
- Tests: `venv/bin/pip install pytest && venv/bin/python -m pytest`. The
  fixtures under `tests/fixtures/` are synthetic: real payload structure
  with every id, name and text replaced. `feed_group.expected.json` pins
  the feed extractor's output; keep it green when touching `scrape.py`.
