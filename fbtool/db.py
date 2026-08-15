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
"""


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # Databases created before delta tracking lack the updated_at column.
    cols = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    if "updated_at" not in cols:
        con.execute("ALTER TABLE posts ADD COLUMN updated_at TEXT")
    return con


def upsert_post(con: sqlite3.Connection, post: dict) -> str:
    """Insert or refresh a post; returns 'new', 'updated', or 'unchanged'.
    An update keeps the superseded text in post_versions."""
    row = con.execute("SELECT text, posted_at FROM posts WHERE id = ?",
                      (post["id"],)).fetchone()
    if row is None:
        con.execute(
            """
            INSERT INTO posts (id, group_slug, author, text, posted_at, permalink, scraped_at)
            VALUES (:id, :group_slug, :author, :text, :posted_at, :permalink, :scraped_at)
            """,
            post,
        )
        return "new"
    if row["posted_at"] is None and post["posted_at"]:
        con.execute("UPDATE posts SET posted_at = ? WHERE id = ?",
                    (post["posted_at"], post["id"]))
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
