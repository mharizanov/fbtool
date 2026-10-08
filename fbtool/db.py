import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,
    group_slug  TEXT NOT NULL,
    author      TEXT,
    text        TEXT,
    posted_at   TEXT,           -- ISO 8601, best-effort parse of FB's relative time
    permalink   TEXT,
    scraped_at  TEXT NOT NULL,  -- when the post was first seen
    updated_at  TEXT            -- when a re-scrape last grew the text (null: never)
);
CREATE INDEX IF NOT EXISTS idx_posts_group_time ON posts (group_slug, posted_at);

-- One row per completed scrape run; delta reports use these as baselines.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL
);

-- Superseded text of updated posts, so a delta can show just what was added.
CREATE TABLE IF NOT EXISTS post_versions (
    post_id     TEXT NOT NULL,
    replaced_at TEXT NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_post ON post_versions (post_id, replaced_at);

-- Where search hits come from: groups, Pages and profiles that are not
-- (necessarily) configured feeds. Keyed like posts.group_slug.
CREATE TABLE IF NOT EXISTS sources (
    slug        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,  -- group | page | author | unknown
    name        TEXT,
    url         TEXT
);

-- One row per distinct search (query text + filters).
CREATE TABLE IF NOT EXISTS queries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    filters     TEXT NOT NULL,  -- canonical JSON
    created_at  TEXT NOT NULL,
    last_run_at TEXT,
    UNIQUE (text, filters)
);

-- Which posts a search returned, in Facebook's result order (rank 1 = top).
CREATE TABLE IF NOT EXISTS search_hits (
    query_id    INTEGER NOT NULL,
    post_id     TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (query_id, post_id)
);

-- Daily load spent on searching (page loads, scrolls, queries).
CREATE TABLE IF NOT EXISTS search_budget (
    day         TEXT PRIMARY KEY,
    page_loads  INTEGER NOT NULL DEFAULT 0,
    scrolls     INTEGER NOT NULL DEFAULT 0,
    queries     INTEGER NOT NULL DEFAULT 0
);
"""


# group_slug of a search hit whose origin couldn't be worked out; any later
# sighting that knows the origin replaces it.
UNKNOWN_ORIGIN = "search"


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # Databases created before delta tracking lack the updated_at column.
    cols = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    if "updated_at" not in cols:
        con.execute("ALTER TABLE posts ADD COLUMN updated_at TEXT")
    for col in ("attachment", "comment_count"):
        if col not in cols:
            con.execute(f"ALTER TABLE posts ADD COLUMN {col} "
                        f"{'INTEGER' if col == 'comment_count' else 'TEXT'}")
    return con


def upsert_post(con: sqlite3.Connection, post: dict, owner: bool = False) -> str:
    """Insert or refresh a post; returns 'new', 'updated', or 'unchanged'.
    An update keeps the superseded text in post_versions.

    `owner` marks a scrape of the post's own configured feed: it is the
    authority on group_slug, so a post a search stored first under another
    origin moves to the feed (otherwise build_corpus, delta and the
    known-posts early stop would never see it). An unknown origin is
    replaced by any known one."""
    row = con.execute("SELECT text, posted_at, group_slug, attachment, comment_count "
                      "FROM posts WHERE id = ?", (post["id"],)).fetchone()
    post = {"attachment": None, "comment_count": None, **post}
    if row is None:
        con.execute(
            """
            INSERT INTO posts (id, group_slug, author, text, posted_at, permalink, scraped_at,
                               attachment, comment_count)
            VALUES (:id, :group_slug, :author, :text, :posted_at, :permalink, :scraped_at,
                    :attachment, :comment_count)
            """,
            post,
        )
        return "new"
    if row["group_slug"] != post["group_slug"] and (
            owner or (row["group_slug"] == UNKNOWN_ORIGIN and post["group_slug"] != UNKNOWN_ORIGIN)):
        con.execute("UPDATE posts SET group_slug = ?, permalink = ? WHERE id = ?",
                    (post["group_slug"], post["permalink"], post["id"]))
    if row["posted_at"] is None and post["posted_at"]:
        con.execute("UPDATE posts SET posted_at = ? WHERE id = ?",
                    (post["posted_at"], post["id"]))
    # Side fields fill in without counting as an update: a re-scrape adding
    # them must not show up as fresh activity in `delta`.
    if row["attachment"] is None and post["attachment"]:
        con.execute("UPDATE posts SET attachment = ? WHERE id = ?",
                    (post["attachment"], post["id"]))
    if post["comment_count"] is not None and post["comment_count"] != row["comment_count"]:
        con.execute("UPDATE posts SET comment_count = ? WHERE id = ?",
                    (post["comment_count"], post["id"]))
    if len(post["text"] or "") > len(row["text"] or ""):
        con.execute(
            "INSERT INTO post_versions (post_id, replaced_at, text) VALUES (?, ?, ?)",
            (post["id"], post["scraped_at"], row["text"] or ""),
        )
        con.execute("UPDATE posts SET text = ?, updated_at = ? WHERE id = ?",
                    (post["text"], post["scraped_at"], post["id"]))
        return "updated"
    return "unchanged"


def record_run(con: sqlite3.Connection, started_at: str) -> None:
    con.execute("INSERT INTO runs (started_at) VALUES (?)", (started_at,))


def run_boundary(con: sqlite3.Connection, runs_back: int = 1) -> str | None:
    """started_at of the runs_back-th most recent run — the point a delta
    report compares against. None when that many runs aren't recorded."""
    rows = con.execute("SELECT started_at FROM runs ORDER BY id DESC LIMIT ?",
                       (runs_back,)).fetchall()
    return rows[-1]["started_at"] if len(rows) >= runs_back else None


def posts_since(con: sqlite3.Connection, group_slug: str, since_iso: str) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT * FROM posts
        WHERE group_slug = ? AND (posted_at >= ? OR posted_at IS NULL AND scraped_at >= ?)
        ORDER BY posted_at
        """,
        (group_slug, since_iso, since_iso),
    ).fetchall()


def new_posts_since(con: sqlite3.Connection, group_slug: str, since_iso: str) -> list[sqlite3.Row]:
    """Posts first seen at or after `since_iso`."""
    return con.execute(
        "SELECT * FROM posts WHERE group_slug = ? AND scraped_at >= ? ORDER BY posted_at",
        (group_slug, since_iso),
    ).fetchall()


def known_post_ids(con: sqlite3.Connection, group_slug: str) -> set[str]:
    """All post IDs already stored for this group — lets a scrape stop
    early once it re-encounters only already-known posts."""
    rows = con.execute("SELECT id FROM posts WHERE group_slug = ?", (group_slug,)).fetchall()
    return {r["id"] for r in rows}


def updated_posts_since(con: sqlite3.Connection, group_slug: str, since_iso: str) -> list[sqlite3.Row]:
    """Posts that existed before `since_iso` but whose text grew after it."""
    return con.execute(
        """
        SELECT * FROM posts
        WHERE group_slug = ? AND scraped_at < ? AND updated_at >= ?
        ORDER BY posted_at
        """,
        (group_slug, since_iso, since_iso),
    ).fetchall()


def text_as_of(con: sqlite3.Connection, post_id: str, since_iso: str) -> str | None:
    """The text a post had just before `since_iso`: the oldest version
    superseded after that point. None when no version was kept."""
    row = con.execute(
        """
        SELECT text FROM post_versions
        WHERE post_id = ? AND replaced_at >= ?
        ORDER BY replaced_at LIMIT 1
        """,
        (post_id, since_iso),
    ).fetchone()
    return row["text"] if row else None


def upsert_source(con: sqlite3.Connection, slug: str, kind: str,
                  name: str | None, url: str | None) -> None:
    con.execute(
        """
        INSERT INTO sources (slug, kind, name, url) VALUES (?, ?, ?, ?)
        ON CONFLICT (slug) DO UPDATE SET
            name = COALESCE(excluded.name, sources.name),
            url = COALESCE(excluded.url, sources.url)
        """,
        (slug, kind, name, url),
    )


def query_id(con: sqlite3.Connection, text: str, filters: str, now_iso: str) -> int:
    con.execute("INSERT OR IGNORE INTO queries (text, filters, created_at) VALUES (?, ?, ?)",
                (text, filters, now_iso))
    return con.execute("SELECT id FROM queries WHERE text = ? AND filters = ?",
                       (text, filters)).fetchone()["id"]


def query_last_run(con: sqlite3.Connection, qid: int) -> str | None:
    return con.execute("SELECT last_run_at FROM queries WHERE id = ?", (qid,)).fetchone()["last_run_at"]


def record_hits(con: sqlite3.Connection, qid: int, ranked_ids: list[str], seen_at: str) -> None:
    """Store a fresh result list. Re-runs replace the rank: Facebook's order
    changes, and the latest one is what the next cached read should see."""
    con.execute("DELETE FROM search_hits WHERE query_id = ?", (qid,))
    con.executemany(
        "INSERT INTO search_hits (query_id, post_id, rank, seen_at) VALUES (?, ?, ?, ?)",
        [(qid, pid, i + 1, seen_at) for i, pid in enumerate(ranked_ids)],
    )
    con.execute("UPDATE queries SET last_run_at = ? WHERE id = ?", (seen_at, qid))


def hits_for(con: sqlite3.Connection, qid: int) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT p.*, h.rank, s.kind AS source_kind, s.name AS source_name, s.url AS source_url
        FROM search_hits h JOIN posts p ON p.id = h.post_id
        LEFT JOIN sources s ON s.slug = p.group_slug
        WHERE h.query_id = ? ORDER BY h.rank
        """,
        (qid,),
    ).fetchall()


def budget_used(con: sqlite3.Connection, day: str) -> dict:
    row = con.execute("SELECT page_loads, scrolls, queries FROM search_budget WHERE day = ?",
                      (day,)).fetchone()
    return dict(row) if row else {"page_loads": 0, "scrolls": 0, "queries": 0}


def budget_add(con: sqlite3.Connection, day: str, page_loads: int = 0,
               scrolls: int = 0, queries: int = 0) -> None:
    con.execute(
        """
        INSERT INTO search_budget (day, page_loads, scrolls, queries) VALUES (?, ?, ?, ?)
        ON CONFLICT (day) DO UPDATE SET
            page_loads = page_loads + excluded.page_loads,
            scrolls = scrolls + excluded.scrolls,
            queries = queries + excluded.queries
        """,
        (day, page_loads, scrolls, queries),
    )
