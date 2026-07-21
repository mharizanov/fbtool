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
    scraped_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_group_time ON posts (group_slug, posted_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert_post(con: sqlite3.Connection, post: dict) -> None:
    con.execute(
        """
        INSERT INTO posts (id, group_slug, author, text, posted_at, permalink, scraped_at)
        VALUES (:id, :group_slug, :author, :text, :posted_at, :permalink, :scraped_at)
        ON CONFLICT(id) DO UPDATE SET
            text      = CASE WHEN length(excluded.text) > length(posts.text)
                             THEN excluded.text ELSE posts.text END,
            posted_at = COALESCE(posts.posted_at, excluded.posted_at)
        """,
        post,
    )


def posts_since(con: sqlite3.Connection, group_slug: str, since_iso: str) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT * FROM posts
        WHERE group_slug = ? AND (posted_at >= ? OR posted_at IS NULL AND scraped_at >= ?)
        ORDER BY posted_at
        """,
        (group_slug, since_iso, since_iso),
    ).fetchall()
