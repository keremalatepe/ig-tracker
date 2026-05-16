"""
Dashboard Data Exporter
=======================
SQLite veritabanını okuyup mobil dostu compact JSON üretir.
Cowork artifact / Chart.js dashboard bu dosyayı raw.githubusercontent.com'dan okur.

Çıktı:
    dashboard_data.json  (root'a yazılır, workflow tarafından commit edilir)

Format:
{
  "generated_at": "ISO",
  "profile": { "username", "name", "followers_count", "follows_count",
               "media_count", "biography", "profile_picture_url" },
  "follower_timeseries": [{"t": "ISO", "v": N}, ...],   // son 90 gün, günlük örneklendi
  "account_insights_timeseries": [
      {"d": "YYYY-MM-DD", "reach": N, "profile_views": N, ...}
  ],
  "online_followers_heatmap": [
      {"d": "YYYY-MM-DD", "h": 0..23, "v": N}
  ],
  "demographics": {
      "age":    [{"k": "18-24", "v": N}, ...],
      "gender": [...],
      "country":[...],
      "city":   [...]
  },
  "posts": [
    {
      "id", "permalink", "thumbnail", "type", "product_type",
      "caption_short" (120ch),
      "posted_at" (ISO),
      "latest": { views, reach, likes, comments, saved, shares,
                 follows, profile_visits, total_interactions,
                 ig_reels_avg_watch_time },
      "history": [ {"t": "ISO", "views": N, "reach": N, ...} ]  // sadece son 30 nokta
    }
  ],
  "stories": [...]
}
"""

import os
import json
import sqlite3
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("IG_DB_PATH", "instagram_data.db")
OUTPUT_PATH = os.environ.get("DASHBOARD_JSON", "dashboard_data.json")

# Limitler
POSTS_LIMIT = 500                 # Son N post
POST_HISTORY_POINTS = 30          # Her post için en son N snapshot
FOLLOWER_LOOKBACK_DAYS = 365      # Takipçi serisi için maksimum
ACCOUNT_LOOKBACK_DAYS = 365
CAPTION_MAX = 150


def safe(conn: sqlite3.Connection, table: str) -> bool:
    """Tablonun var olup olmadığını kontrol et."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def columns_of(conn: sqlite3.Connection, table: str) -> set:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def get_latest_profile(conn: sqlite3.Connection) -> dict:
    row = conn.execute("""
        SELECT username, name, biography, followers_count, follows_count,
               media_count, profile_picture_url, website, fetched_at
        FROM profile_snapshots
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    if not row:
        return {}
    cols = ["username", "name", "biography", "followers_count", "follows_count",
            "media_count", "profile_picture_url", "website", "fetched_at"]
    profile = dict(zip(cols, row))

    # Daily delta (son 30 gün)
    profile["follower_change_7d"] = follower_change(conn, days=7)
    profile["follower_change_30d"] = follower_change(conn, days=30)
    profile["follower_change_90d"] = follower_change(conn, days=90)
    return profile


def follower_change(conn: sqlite3.Connection, days: int) -> int | None:
    """Şu anki takipçi - N gün önceki takipçi."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    row = conn.execute("""
        SELECT
          (SELECT followers_count FROM profile_snapshots ORDER BY id DESC LIMIT 1)
          -
          (SELECT followers_count FROM profile_snapshots
           WHERE fetched_at <= ? ORDER BY id DESC LIMIT 1)
    """, (cutoff,)).fetchone()
    return row[0] if row and row[0] is not None else None


def get_follower_timeseries(conn: sqlite3.Connection, days: int) -> list:
    """Günde 1 örnek (son snapshot/gün), son N gün."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT date(fetched_at) AS d, MAX(fetched_at), followers_count
        FROM profile_snapshots
        WHERE fetched_at >= ? AND followers_count IS NOT NULL
        GROUP BY date(fetched_at)
        ORDER BY d
    """, (cutoff,)).fetchall()
    return [{"t": r[0], "v": r[2]} for r in rows]


def get_account_insights_series(conn: sqlite3.Connection, days: int) -> list:
    if not safe(conn, "account_insights"):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = conn.execute("""
        SELECT period_date, reach, profile_views, accounts_engaged,
               total_interactions, follower_count, website_clicks
        FROM account_insights
        WHERE period_date >= ?
        ORDER BY period_date
    """, (cutoff,)).fetchall()
    return [
        {"d": r[0], "reach": r[1], "profile_views": r[2], "accounts_engaged": r[3],
         "total_interactions": r[4], "follower_count": r[5], "website_clicks": r[6]}
        for r in rows
    ]


def get_online_followers(conn: sqlite3.Connection) -> list:
    if not safe(conn, "online_followers"):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    rows = conn.execute("""
        SELECT period_date, hour, value
        FROM online_followers
        WHERE period_date >= ?
        ORDER BY period_date, hour
    """, (cutoff,)).fetchall()
    return [{"d": r[0], "h": r[1], "v": r[2]} for r in rows]


def get_demographics(conn: sqlite3.Connection) -> dict:
    if not safe(conn, "audience_demographics"):
        return {}
    # En son fetch tarihini al
    row = conn.execute("""
        SELECT fetched_at FROM audience_demographics
        ORDER BY fetched_at DESC LIMIT 1
    """).fetchone()
    if not row:
        return {}
    latest_at = row[0]
    rows = conn.execute("""
        SELECT breakdown, dimension, value
        FROM audience_demographics
        WHERE fetched_at = ?
        ORDER BY breakdown, value DESC
    """, (latest_at,)).fetchall()

    out = {}
    for breakdown, dimension, value in rows:
        out.setdefault(breakdown, []).append({"k": dimension, "v": value})
    return out


def get_posts(conn: sqlite3.Connection, limit: int = POSTS_LIMIT) -> list:
    """Postlar + her birinin son N snapshot'ı."""
    rows = conn.execute(f"""
        SELECT post_id, permalink, thumbnail_url, media_type, media_product_type,
               caption, timestamp
        FROM posts
        ORDER BY timestamp DESC
        LIMIT {limit}
    """).fetchall()

    post_ids = [r[0] for r in rows]
    if not post_ids:
        return []

    # En son snapshot'ları toplu çek
    placeholders = ",".join("?" * len(post_ids))
    latest_snapshot_rows = conn.execute(f"""
        SELECT s.* FROM post_snapshots s
        INNER JOIN (
            SELECT post_id, MAX(id) AS max_id
            FROM post_snapshots
            WHERE post_id IN ({placeholders})
            GROUP BY post_id
        ) m ON s.id = m.max_id
    """, post_ids).fetchall()

    # Kolon adlarını al
    snap_cols = [d[0] for d in conn.execute("SELECT * FROM post_snapshots LIMIT 0").description]
    latest_by_id = {row[snap_cols.index("post_id")]: dict(zip(snap_cols, row))
                    for row in latest_snapshot_rows}

    # Her post için son N nokta history
    posts_out = []
    for r in rows:
        post_id, permalink, thumb, media_type, product_type, caption, ts = r
        latest = latest_by_id.get(post_id, {})

        history_rows = conn.execute(f"""
            SELECT fetched_at, views, reach, like_count, comments_count,
                   saved, shares, total_interactions, follows, profile_visits,
                   ig_reels_avg_watch_time
            FROM post_snapshots
            WHERE post_id = ?
            ORDER BY id DESC
            LIMIT {POST_HISTORY_POINTS}
        """, (post_id,)).fetchall()

        history = [
            {"t": h[0], "views": h[1], "reach": h[2], "likes": h[3],
             "comments": h[4], "saved": h[5], "shares": h[6],
             "total_interactions": h[7], "follows": h[8],
             "profile_visits": h[9], "ig_reels_avg_watch_time": h[10]}
            for h in reversed(history_rows)
        ]

        short_caption = (caption or "")
        if len(short_caption) > CAPTION_MAX:
            short_caption = short_caption[:CAPTION_MAX].rstrip() + "…"

        posts_out.append({
            "id": post_id,
            "permalink": permalink,
            "thumbnail": thumb,
            "type": media_type,
            "product_type": product_type,
            "caption_short": short_caption,
            "posted_at": ts,
            "latest": {
                "views": latest.get("views"),
                "reach": latest.get("reach"),
                "likes": latest.get("like_count"),
                "comments": latest.get("comments_count"),
                "saved": latest.get("saved"),
                "shares": latest.get("shares"),
                "total_interactions": latest.get("total_interactions"),
                "follows": latest.get("follows"),
                "profile_visits": latest.get("profile_visits"),
                "profile_activity": latest.get("profile_activity"),
                "ig_reels_avg_watch_time": latest.get("ig_reels_avg_watch_time"),
                "ig_reels_video_view_total_time": latest.get("ig_reels_video_view_total_time"),
                "clips_replays_count": latest.get("clips_replays_count"),
                "fetched_at": latest.get("fetched_at"),
            },
            "history": history,
        })
    return posts_out


def get_stories(conn: sqlite3.Connection) -> list:
    if not safe(conn, "stories"):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    rows = conn.execute("""
        SELECT s.story_id, s.permalink, s.thumbnail_url, s.timestamp, s.first_seen_at
        FROM stories s
        WHERE s.timestamp >= ? OR s.first_seen_at >= ?
        ORDER BY s.timestamp DESC
        LIMIT 200
    """, (cutoff, cutoff)).fetchall()

    out = []
    for r in rows:
        story_id, permalink, thumb, ts, first_seen = r
        snap = conn.execute("""
            SELECT reach, replies, taps_forward, taps_back, exits, views, total_interactions
            FROM story_snapshots
            WHERE story_id = ?
            ORDER BY id DESC LIMIT 1
        """, (story_id,)).fetchone()
        snap_data = {}
        if snap:
            snap_data = {"reach": snap[0], "replies": snap[1], "taps_forward": snap[2],
                         "taps_back": snap[3], "exits": snap[4], "views": snap[5],
                         "total_interactions": snap[6]}
        out.append({
            "id": story_id,
            "permalink": permalink,
            "thumbnail": thumb,
            "posted_at": ts,
            "first_seen_at": first_seen,
            "latest": snap_data,
        })
    return out


def get_fetch_runs(conn: sqlite3.Connection) -> list:
    if not safe(conn, "fetch_runs"):
        return []
    rows = conn.execute("""
        SELECT started_at, completed_at, mode, status,
               posts_fetched, stories_fetched, api_requests
        FROM fetch_runs
        ORDER BY id DESC
        LIMIT 50
    """).fetchall()
    return [
        {"started": r[0], "completed": r[1], "mode": r[2], "status": r[3],
         "posts": r[4], "stories": r[5], "api_requests": r[6]}
        for r in rows
    ]


def build_payload() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": 2,
            "profile": get_latest_profile(conn),
            "follower_timeseries": get_follower_timeseries(conn, days=FOLLOWER_LOOKBACK_DAYS),
            "account_insights_timeseries": get_account_insights_series(conn, days=ACCOUNT_LOOKBACK_DAYS),
            "online_followers_heatmap": get_online_followers(conn),
            "demographics": get_demographics(conn),
            "posts": get_posts(conn),
            "stories": get_stories(conn),
            "recent_runs": get_fetch_runs(conn),
        }
        return payload
    finally:
        conn.close()


def main():
    payload = build_payload()
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = os.path.getsize(OUTPUT_PATH) / 1024
    print(f"[export] {OUTPUT_PATH} yazıldı ({size_kb:.1f} KB) | "
          f"{len(payload.get('posts', []))} post, "
          f"{len(payload.get('stories', []))} story, "
          f"profile.followers={payload.get('profile', {}).get('followers_count')}")


if __name__ == "__main__":
    main()
