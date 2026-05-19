"""
YouTube Schema Migration
========================
youtube_data.db tablolarını oluşturur / günceller.
Idempotent: birden fazla kez çalıştırılabilir.

Çalıştırma:
    python migrate_yt_schema.py
"""

import os
import sqlite3
import logging

DB_PATH = os.environ.get("YT_DB_PATH", "youtube_data.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("migrate_yt")


def add_column_if_missing(cur: sqlite3.Cursor, table: str, column: str, coltype: str):
    cur.execute(f"PRAGMA table_info({table})")
    if not any(row[1] == column for row in cur.fetchall()):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        log.info(f"  + {table}.{column} ({coltype}) eklendi")


def migrate(conn: sqlite3.Connection):
    cur = conn.cursor()

    log.info("yt_channel_snapshots tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS yt_channel_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        channel_title TEXT,
        subscriber_count INTEGER,
        view_count INTEGER,
        video_count INTEGER,
        hidden_subscriber_count INTEGER
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_yt_channel_fetched ON yt_channel_snapshots(fetched_at)")
    add_column_if_missing(cur, "yt_channel_snapshots", "thumbnail_url", "TEXT")

    log.info("yt_videos tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS yt_videos (
        video_id TEXT PRIMARY KEY,
        title TEXT,
        description TEXT,
        published_at TEXT,
        thumbnail_url TEXT,
        duration_seconds INTEGER,
        is_short INTEGER DEFAULT 0,
        tags TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_yt_videos_published ON yt_videos(published_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_yt_videos_is_short ON yt_videos(is_short)")

    log.info("yt_video_snapshots tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS yt_video_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        video_id TEXT NOT NULL,
        views INTEGER,
        public_views INTEGER,
        likes INTEGER,
        comments INTEGER,
        shares INTEGER,
        estimated_minutes_watched INTEGER,
        average_view_duration INTEGER,
        average_view_percentage REAL,
        impressions INTEGER,
        impressions_ctr REAL,
        subscribers_gained INTEGER,
        subscribers_lost INTEGER,
        FOREIGN KEY(video_id) REFERENCES yt_videos(video_id)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_yt_snap_video ON yt_video_snapshots(video_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_yt_snap_fetched ON yt_video_snapshots(fetched_at)")
    add_column_if_missing(cur, "yt_video_snapshots", "public_views", "INTEGER")

    log.info("yt_video_daily tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS yt_video_daily (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL,
        date TEXT NOT NULL,
        views INTEGER,
        likes INTEGER,
        comments INTEGER,
        shares INTEGER,
        estimated_minutes_watched INTEGER,
        average_view_duration INTEGER,
        average_view_percentage REAL,
        impressions INTEGER,
        impressions_ctr REAL,
        subscribers_gained INTEGER,
        subscribers_lost INTEGER,
        UNIQUE(video_id, date),
        FOREIGN KEY(video_id) REFERENCES yt_videos(video_id)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_yt_daily_video ON yt_video_daily(video_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_yt_daily_date ON yt_video_daily(date)")

    log.info("yt_fetch_runs tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS yt_fetch_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        mode TEXT NOT NULL,
        status TEXT,
        videos_fetched INTEGER,
        api_requests INTEGER,
        error_message TEXT
    )
    """)

    log.info("yt_fetch_cursors tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS yt_fetch_cursors (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    conn.commit()
    log.info("Migration tamamlandı.")


def main():
    log.info(f"DB: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        migrate(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
