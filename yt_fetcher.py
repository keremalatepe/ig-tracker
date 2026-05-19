"""
YouTube Data Fetcher
====================
YouTube kanalından Shorts verilerini çeker, youtube_data.db'ye kaydeder.
Instagram fetcher ile aynı tiered yapı.

Tiered fetching:
- hourly : son 14 gündeki videolar + kanal snapshot          (her saat)
- full   : tüm videolar (ilk kurulum veya manuel tetikleme)

Kullanım:
    python yt_fetcher.py                → auto mode
    python yt_fetcher.py --mode full    → tüm videolar
    python yt_fetcher.py --mode hourly  → sadece son 14 gün

GitHub Actions için ortam değişkenleri:
    YOUTUBE_CLIENT_ID
    YOUTUBE_CLIENT_SECRET
    YOUTUBE_REFRESH_TOKEN
"""

import os
import re
import sys
import json
import time
import sqlite3
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("requests kütüphanesi gerekli: pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Ayarlar ───────────────────────────────────────────────
DB_PATH = os.environ.get("YT_DB_PATH", "youtube_data.db")
YT_TOKEN_FILE = Path("yt_token.json")

DATA_API_BASE = "https://www.googleapis.com/youtube/v3"
ANALYTICS_API_BASE = "https://youtubeanalytics.googleapis.com/v2"
TOKEN_URL = "https://oauth2.googleapis.com/token"

HOURLY_LOOKBACK_DAYS = 14
REQUEST_DELAY_SECONDS = 0.2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("yt_fetcher")


# ─── ISO 8601 duration → saniye ────────────────────────────
def parse_duration(iso: str) -> int:
    """PT1M30S → 90, PT45S → 45, PT2H → 7200"""
    if not iso:
        return 0
    match = re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso
    )
    if not match:
        return 0
    days, hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def is_short(duration_seconds: int, title: str = "", description: str = "") -> bool:
    if duration_seconds <= 0 or duration_seconds > 60:
        return False
    return True


# ─── Token Yönetimi ────────────────────────────────────────
class YouTubeTokenManager:

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.access_token: str | None = None
        self.expires_at: float = 0

        if YT_TOKEN_FILE.exists():
            try:
                data = json.loads(YT_TOKEN_FILE.read_text())
                self.access_token = data.get("access_token")
                self.expires_at = data.get("expires_at", 0)
                log.info("YouTube token dosyadan yüklendi.")
            except Exception:
                pass

    def _refresh(self):
        log.info("YouTube access token yenileniyor...")
        resp = requests.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["access_token"]
        self.expires_at = time.time() + data.get("expires_in", 3600) - 60
        YT_TOKEN_FILE.write_text(json.dumps({
            "access_token": self.access_token,
            "expires_at": self.expires_at,
        }, indent=2))
        log.info("Token yenilendi.")

    def get_token(self) -> str:
        if not self.access_token or time.time() >= self.expires_at:
            self._refresh()
        return self.access_token


# ─── API İstekleri ─────────────────────────────────────────
class YouTubeFetcher:

    def __init__(self, token_manager: YouTubeTokenManager):
        self.tm = token_manager
        self.request_count = 0

    def _get(self, url: str, params: dict, retries: int = 3) -> dict:
        params = {**params, "access_token": self.tm.get_token()}
        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params, timeout=30)
                self.request_count += 1
                if REQUEST_DELAY_SECONDS:
                    time.sleep(REQUEST_DELAY_SECONDS)

                if resp.status_code == 401:
                    log.warning("401 — token yenileniyor...")
                    self.tm.access_token = None
                    params["access_token"] = self.tm.get_token()
                    continue

                if resp.status_code == 429:
                    wait = 60 * (2 ** attempt)
                    log.warning(f"Rate limit! {wait}s bekleniyor...")
                    time.sleep(wait)
                    continue

                if resp.status_code == 403:
                    data = resp.json()
                    err = data.get("error", {})
                    reason = err.get("errors", [{}])[0].get("reason", "")
                    if reason == "quotaExceeded":
                        log.error("YouTube API kotası doldu! Bugün daha fazla istek yapılamaz.")
                        return {"error": {"message": "quotaExceeded"}}
                    return {"error": err}

                return resp.json()

            except requests.exceptions.RequestException as e:
                log.warning(f"Network hatası (deneme {attempt + 1}/{retries}): {e}")
                time.sleep(5 * (2 ** attempt))

        return {"error": {"message": "Tüm denemeler başarısız"}}

    # ── Kanal bilgisi ──
    def fetch_channel(self) -> dict:
        data = self._get(f"{DATA_API_BASE}/channels", {
            "part": "snippet,statistics",
            "mine": "true",
        })
        items = data.get("items", [])
        if not items:
            return {}
        item = items[0]
        stats = item.get("statistics", {})
        snippet = item.get("snippet", {})
        return {
            "channel_id": item["id"],
            "channel_title": snippet.get("title"),
            "subscriber_count": int(stats.get("subscriberCount", 0) or 0),
            "view_count": int(stats.get("viewCount", 0) or 0),
            "video_count": int(stats.get("videoCount", 0) or 0),
            "hidden_subscriber_count": 1 if stats.get("hiddenSubscriberCount") else 0,
        }

    # ── Video listesi ──
    def fetch_channel_videos(self, channel_id: str, published_after: str | None = None) -> list:
        """Kanaldaki tüm videoları döndürür (sayfalama ile)."""
        videos = []
        params = {
            "part": "id",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "maxResults": 50,
        }
        if published_after:
            params["publishedAfter"] = published_after

        url = f"{DATA_API_BASE}/search"
        while url:
            data = self._get(url, params)
            if "error" in data:
                log.warning(f"Video listesi hatası: {data['error']}")
                break
            for item in data.get("items", []):
                vid_id = item.get("id", {}).get("videoId")
                if vid_id:
                    videos.append(vid_id)

            next_token = data.get("nextPageToken")
            if next_token:
                params = {"pageToken": next_token}
                url = f"{DATA_API_BASE}/search"
            else:
                break

        log.info(f"  {len(videos)} video ID alındı.")
        return videos

    # ── Video detayları ──
    def fetch_video_details(self, video_ids: list) -> list:
        """En fazla 50 ID'lik batch'lerle video detaylarını çeker."""
        results = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            data = self._get(f"{DATA_API_BASE}/videos", {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(batch),
            })
            if "error" in data:
                log.warning(f"Video detayları hatası: {data['error']}")
                continue
            results.extend(data.get("items", []))
        return results

    # ── Analytics: tek video, günlük breakdown ──
    def fetch_video_analytics_daily(self, video_id: str, channel_id: str,
                                     start_date: str, end_date: str) -> list:
        """dimensions=day ile günlük satırlar döndürür. full mode / geçmiş için."""
        metrics = ",".join([
            "views", "likes", "dislikes", "comments", "shares",
            "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
            "impressions", "impressionsClickThroughRate",
            "subscribersGained", "subscribersLost",
        ])
        data = self._get(f"{ANALYTICS_API_BASE}/reports", {
            "ids": f"channel=={channel_id}",
            "startDate": start_date,
            "endDate": end_date,
            "metrics": metrics,
            "dimensions": "day",
            "filters": f"video=={video_id}",
            "sort": "day",
        })
        if "error" in data:
            log.debug(f"  Daily analytics alınamadı ({video_id}): {data['error']}")
            return []

        col_names = [h["name"] for h in data.get("columnHeaders", [])]
        return [dict(zip(col_names, row)) for row in data.get("rows", [])]

    # ── Analytics: tek video, toplam ──
    def fetch_video_analytics(self, video_id: str, channel_id: str,
                               start_date: str, end_date: str) -> dict:
        metrics = ",".join([
            "views", "likes", "dislikes", "comments", "shares",
            "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
            "impressions", "impressionsClickThroughRate",
            "subscribersGained", "subscribersLost",
        ])
        data = self._get(f"{ANALYTICS_API_BASE}/reports", {
            "ids": f"channel=={channel_id}",
            "startDate": start_date,
            "endDate": end_date,
            "metrics": metrics,
            "filters": f"video=={video_id}",
            "dimensions": "",
        })
        if "error" in data:
            log.debug(f"  Analytics alınamadı ({video_id}): {data['error']}")
            return {}

        rows = data.get("rows", [])
        if not rows:
            return {}

        col_names = [h["name"] for h in data.get("columnHeaders", [])]
        return dict(zip(col_names, rows[0]))


# ─── Veritabanı işlemleri ──────────────────────────────────
def insert_channel_snapshot(conn: sqlite3.Connection, fetched_at: str, channel: dict):
    conn.execute("""
    INSERT INTO yt_channel_snapshots (
        fetched_at, channel_id, channel_title,
        subscriber_count, view_count, video_count, hidden_subscriber_count
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        fetched_at,
        channel.get("channel_id"),
        channel.get("channel_title"),
        channel.get("subscriber_count"),
        channel.get("view_count"),
        channel.get("video_count"),
        channel.get("hidden_subscriber_count"),
    ))


def upsert_video(conn: sqlite3.Connection, item: dict):
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    duration_sec = parse_duration(content.get("duration", ""))
    title = snippet.get("title", "")
    desc = snippet.get("description", "")
    tags = json.dumps(snippet.get("tags", []), ensure_ascii=False)
    thumb = (snippet.get("thumbnails", {}).get("high") or
             snippet.get("thumbnails", {}).get("default") or {}).get("url")

    conn.execute("""
    INSERT INTO yt_videos (
        video_id, title, description, published_at,
        thumbnail_url, duration_seconds, is_short, tags
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(video_id) DO UPDATE SET
        title=excluded.title,
        description=excluded.description,
        thumbnail_url=excluded.thumbnail_url,
        duration_seconds=excluded.duration_seconds,
        is_short=excluded.is_short,
        tags=excluded.tags
    """, (
        item["id"],
        title,
        desc,
        snippet.get("publishedAt"),
        thumb,
        duration_sec,
        1 if is_short(duration_sec, title, desc) else 0,
        tags,
    ))


def insert_video_snapshot(conn: sqlite3.Connection, fetched_at: str,
                          video_id: str, analytics: dict):
    def _int(k): return int(analytics[k]) if analytics.get(k) is not None else None
    def _float(k): return float(analytics[k]) if analytics.get(k) is not None else None

    conn.execute("""
    INSERT INTO yt_video_snapshots (
        fetched_at, video_id,
        views, likes, comments, shares,
        estimated_minutes_watched, average_view_duration, average_view_percentage,
        impressions, impressions_ctr,
        subscribers_gained, subscribers_lost
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        fetched_at, video_id,
        _int("views"), _int("likes"), _int("comments"), _int("shares"),
        _int("estimatedMinutesWatched"), _int("averageViewDuration"),
        _float("averageViewPercentage"),
        _int("impressions"), _float("impressionsClickThroughRate"),
        _int("subscribersGained"), _int("subscribersLost"),
    ))


def insert_video_daily(conn: sqlite3.Connection, video_id: str, row: dict):
    """Günlük tarihsel veriyi upsert eder. UNIQUE(video_id, date) çakışırsa günceller."""
    def _int(k): return int(row[k]) if row.get(k) is not None else None
    def _float(k): return float(row[k]) if row.get(k) is not None else None

    conn.execute("""
    INSERT INTO yt_video_daily (
        video_id, date,
        views, likes, comments, shares,
        estimated_minutes_watched, average_view_duration, average_view_percentage,
        impressions, impressions_ctr,
        subscribers_gained, subscribers_lost
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(video_id, date) DO UPDATE SET
        views=excluded.views, likes=excluded.likes,
        comments=excluded.comments, shares=excluded.shares,
        estimated_minutes_watched=excluded.estimated_minutes_watched,
        average_view_duration=excluded.average_view_duration,
        average_view_percentage=excluded.average_view_percentage,
        impressions=excluded.impressions, impressions_ctr=excluded.impressions_ctr,
        subscribers_gained=excluded.subscribers_gained,
        subscribers_lost=excluded.subscribers_lost
    """, (
        video_id, row.get("day"),
        _int("views"), _int("likes"), _int("comments"), _int("shares"),
        _int("estimatedMinutesWatched"), _int("averageViewDuration"),
        _float("averageViewPercentage"),
        _int("impressions"), _float("impressionsClickThroughRate"),
        _int("subscribersGained"), _int("subscribersLost"),
    ))


def get_cursor(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM yt_fetch_cursors WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_cursor(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO yt_fetch_cursors (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def log_fetch_run(conn: sqlite3.Connection, started_at: str, completed_at: str,
                  mode: str, status: str, videos: int, requests: int, error: str | None):
    conn.execute("""
    INSERT INTO yt_fetch_runs (started_at, completed_at, mode, status,
                               videos_fetched, api_requests, error_message)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (started_at, completed_at, mode, status, videos, requests, error))


# ─── Çalışma modları ───────────────────────────────────────
def run_fetch(fetcher: YouTubeFetcher, conn: sqlite3.Connection,
              fetched_at: str, mode: str) -> int:
    """Kanal + video + analytics çeker. mode: 'hourly' veya 'full'."""

    # Kanal snapshot
    log.info("[yt] Kanal bilgileri çekiliyor...")
    channel = fetcher.fetch_channel()
    if not channel:
        log.error("Kanal bilgisi alınamadı. Credentials'ı kontrol et.")
        return 0
    channel_id = channel["channel_id"]
    insert_channel_snapshot(conn, fetched_at, channel)
    log.info(f"  #{channel['channel_title']} | Abone: {channel['subscriber_count']} | "
             f"Video: {channel['video_count']}")

    # Video listesi
    published_after = None
    if mode == "hourly":
        cutoff = datetime.now(timezone.utc) - timedelta(days=HOURLY_LOOKBACK_DAYS)
        published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info(f"[yt] Son {HOURLY_LOOKBACK_DAYS} gündeki videolar çekiliyor...")
    else:
        log.info("[yt] Tüm videolar çekiliyor (full mode)...")

    video_ids = fetcher.fetch_channel_videos(channel_id, published_after)
    if not video_ids:
        log.info("[yt] Video bulunamadı.")
        return 0

    # Video detayları
    log.info(f"[yt] {len(video_ids)} video için detaylar çekiliyor...")
    video_items = fetcher.fetch_video_details(video_ids)

    for item in video_items:
        upsert_video(conn, item)

    # Sadece Shorts için analytics çek
    shorts_items = [
        item for item in video_items
        if is_short(parse_duration(item.get("contentDetails", {}).get("duration", "")))
    ]
    log.info(f"[yt] {len(shorts_items)} Short tespit edildi, analytics çekiliyor...")

    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if mode == "hourly":
        start_date = (datetime.now(timezone.utc) - timedelta(days=HOURLY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    else:
        start_date = "2020-01-01"

    count = 0
    for i, item in enumerate(shorts_items, 1):
        video_id = item["id"]
        try:
            if mode == "full":
                # Tüm geçmişi günlük granülasyonda çek
                published = item.get("snippet", {}).get("publishedAt", "2020-01-01")[:10]
                daily_rows = fetcher.fetch_video_analytics_daily(
                    video_id, channel_id, published, end_date
                )
                for row in daily_rows:
                    insert_video_daily(conn, video_id, row)
                # Ayrıca anlık snapshot da al
                analytics = fetcher.fetch_video_analytics(video_id, channel_id, start_date, end_date)
                insert_video_snapshot(conn, fetched_at, video_id, analytics)
                log.debug(f"  {video_id}: {len(daily_rows)} günlük kayıt")
            else:
                # hourly: sadece anlık snapshot
                analytics = fetcher.fetch_video_analytics(video_id, channel_id, start_date, end_date)
                insert_video_snapshot(conn, fetched_at, video_id, analytics)
            count += 1
        except Exception as e:
            log.warning(f"  Video atlandı ({video_id}): {e}")
        if i % 10 == 0:
            log.info(f"  İlerleme: {i}/{len(shorts_items)} | API: {fetcher.request_count}")

    conn.commit()
    log.info(f"[yt] Bitti: {count} Short analytics kaydedildi.")
    return count


def execute_mode(fetcher: YouTubeFetcher, conn: sqlite3.Connection, mode: str):
    fetched_at = datetime.now(timezone.utc).isoformat()
    fetcher.request_count = 0
    videos = 0
    error = None

    try:
        actual_mode = "hourly" if mode == "auto" else mode
        videos = run_fetch(fetcher, conn, fetched_at, actual_mode)
        set_cursor(conn, "last_fetch", fetched_at)
        status = "success"
    except Exception as e:
        error = str(e)
        status = "error"
        log.exception(f"Hata: {e}")

    completed_at = datetime.now(timezone.utc).isoformat()
    log_fetch_run(conn, fetched_at, completed_at, mode, status,
                  videos, fetcher.request_count, error)
    conn.commit()
    log.info(f"Mod '{mode}' bitti. Videos: {videos}, API: {fetcher.request_count}, Status: {status}")


def main():
    parser = argparse.ArgumentParser(description="YouTube Data Fetcher")
    parser.add_argument("--mode", default="auto",
                        choices=["auto", "hourly", "full"],
                        help="auto/hourly: son 14 gün | full: tüm videolar")
    args = parser.parse_args()

    client_id = os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
    refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        print("""
╔══════════════════════════════════════════════════════════════╗
║  .env dosyası oluştur veya ortam değişkenlerini ayarla:     ║
║    YOUTUBE_CLIENT_ID                                         ║
║    YOUTUBE_CLIENT_SECRET                                     ║
║    YOUTUBE_REFRESH_TOKEN                                     ║
╚══════════════════════════════════════════════════════════════╝
        """)
        sys.exit(1)

    try:
        from migrate_yt_schema import migrate
        conn = sqlite3.connect(DB_PATH)
        migrate(conn)
    except ImportError:
        conn = sqlite3.connect(DB_PATH)
        log.warning("migrate_yt_schema.py bulunamadı.")

    token_manager = YouTubeTokenManager(client_id, client_secret, refresh_token)
    fetcher = YouTubeFetcher(token_manager)
    execute_mode(fetcher, conn, args.mode)
    conn.close()


if __name__ == "__main__":
    main()
