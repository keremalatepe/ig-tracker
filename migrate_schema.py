"""
Schema Migration
================
instagram_data.db'yi yeni metriklere uyumlu hale getirir.
Idempotent: birden fazla kez çalıştırılabilir, mevcut veriyi bozmaz.

Yapılanlar:
- post_snapshots tablosuna yeni metrik kolonları ekler (follows, profile_visits, ...)
- profile_snapshots tablosuna ek alanlar ekler (profile_picture_url, vs.)
- Yeni tablolar oluşturur: account_insights, audience_demographics, story_snapshots,
  stories, fetch_runs, fetch_cursors

Çalıştırma:
    python migrate_schema.py
"""

import os
import sqlite3
import logging

DB_PATH = os.environ.get("IG_DB_PATH", "instagram_data.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("migrate")


def column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def add_column_if_missing(cur: sqlite3.Cursor, table: str, column: str, coltype: str):
    if not column_exists(cur, table, column):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        log.info(f"  + {table}.{column} ({coltype}) eklendi")


def migrate(conn: sqlite3.Connection):
    cur = conn.cursor()

    # ─── 1. Mevcut tablolara yeni kolonlar ──────────────────────
    log.info("post_snapshots: yeni kolonlar...")
    add_column_if_missing(cur, "post_snapshots", "follows", "INTEGER")
    add_column_if_missing(cur, "post_snapshots", "profile_visits", "INTEGER")
    add_column_if_missing(cur, "post_snapshots", "profile_activity", "INTEGER")
    add_column_if_missing(cur, "post_snapshots", "ig_reels_avg_watch_time", "INTEGER")
    add_column_if_missing(cur, "post_snapshots", "ig_reels_video_view_total_time", "INTEGER")
    add_column_if_missing(cur, "post_snapshots", "clips_replays_count", "INTEGER")
    add_column_if_missing(cur, "post_snapshots", "ig_reels_aggregated_all_plays_count", "INTEGER")

    log.info("profile_snapshots: yeni kolonlar...")
    add_column_if_missing(cur, "profile_snapshots", "profile_picture_url", "TEXT")
    add_column_if_missing(cur, "profile_snapshots", "website", "TEXT")

    # ─── 2. Yeni tablolar ────────────────────────────────────────
    log.info("account_insights tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS account_insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        period_date TEXT NOT NULL,
        reach INTEGER,
        impressions INTEGER,
        profile_views INTEGER,
        accounts_engaged INTEGER,
        total_interactions INTEGER,
        follower_count INTEGER,
        website_clicks INTEGER,
        email_contacts INTEGER,
        phone_call_clicks INTEGER,
        text_message_clicks INTEGER,
        get_directions_clicks INTEGER,
        UNIQUE(period_date)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_account_insights_date ON account_insights(period_date)")

    log.info("audience_demographics tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audience_demographics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        breakdown TEXT NOT NULL,
        dimension TEXT NOT NULL,
        value INTEGER,
        UNIQUE(fetched_at, breakdown, dimension)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_demographics_fetched ON audience_demographics(fetched_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_demographics_breakdown ON audience_demographics(breakdown)")

    log.info("online_followers tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS online_followers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        period_date TEXT NOT NULL,
        hour INTEGER NOT NULL,
        value INTEGER,
        UNIQUE(period_date, hour)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_online_date ON online_followers(period_date)")

    log.info("stories tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stories (
        story_id TEXT PRIMARY KEY,
        media_type TEXT,
        permalink TEXT,
        timestamp TEXT,
        thumbnail_url TEXT,
        media_url TEXT,
        first_seen_at TEXT NOT NULL
    )
    """)

    log.info("story_snapshots tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS story_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        story_id TEXT NOT NULL,
        reach INTEGER,
        replies INTEGER,
        taps_forward INTEGER,
        taps_back INTEGER,
        exits INTEGER,
        views INTEGER,
        total_interactions INTEGER,
        navigation INTEGER,
        FOREIGN KEY(story_id) REFERENCES stories(story_id)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_story_snapshots_story ON story_snapshots(story_id)")
    log.info("story_snapshots: yeni kolonlar...")
    add_column_if_missing(cur, "story_snapshots", "navigation", "INTEGER")
    log.info("fetch_runs tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS fetch_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        mode TEXT NOT NULL,
        status TEXT,
        posts_fetched INTEGER,
        stories_fetched INTEGER,
        api_requests INTEGER,
        error_message TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_fetch_runs_started ON fetch_runs(started_at)")

    log.info("fetch_cursors tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS fetch_cursors (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    log.info("follower_daily tablosu...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS follower_daily (
        date TEXT PRIMARY KEY,
        followers_count INTEGER,
        followers_delta INTEGER,
        follows INTEGER,
        unfollows INTEGER,
        source TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_follower_daily_date ON follower_daily(date)")
    add_column_if_missing(cur, "follower_daily", "follows", "INTEGER")
    add_column_if_missing(cur, "follower_daily", "unfollows", "INTEGER")

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
