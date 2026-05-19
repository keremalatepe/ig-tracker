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
CHANNEL_ANALYTICS_LOOKBACK_DAYS = 30
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
    if duration_seconds <= 0 or duration_seconds > 180:
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

    def fetch_public_video_stats(self, video_id: str) -> dict:
        """Public Shorts page shows the live counters users actually see."""
        headers = {"User-Agent": "Mozilla/5.0"}
        urls = [
            f"https://www.youtube.com/shorts/{video_id}",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        stats = {"publicViews": None, "publicLikes": None}
        for url in urls:
            try:
                resp = requests.get(url, headers=headers, timeout=30)
                self.request_count += 1
                if REQUEST_DELAY_SECONDS:
                    time.sleep(REQUEST_DELAY_SECONDS)
                resp.raise_for_status()
                if stats["publicViews"] is None:
                    match = re.search(r'"viewCount":"(\d+)"', resp.text)
                    if match:
                        stats["publicViews"] = int(match.group(1))
                if stats["publicLikes"] is None:
                    like_match = re.search(r'"likeCount":"(\d+)"', resp.text)
                    if like_match:
                        stats["publicLikes"] = int(like_match.group(1))
                if stats["publicViews"] is not None and stats["publicLikes"] is not None:
                    break
            except requests.exceptions.RequestException as exc:
                log.debug(f"Public view scrape başarısız ({video_id}): {exc}")
        return stats

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
        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = (thumbnails.get("high") or thumbnails.get("medium") or thumbnails.get("default") or {}).get("url")
        return {
            "channel_id": item["id"],
            "channel_title": snippet.get("title"),
            "channel_handle": snippet.get("customUrl"),  # e.g. "@aybiksbites"
            "thumbnail_url": thumbnail_url,
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
        """dimensions=day ile günlük satırlar döndürür. full mode / geçmiş için.
        Not: impressions/CTR video+day kombinasyonunda desteklenmiyor, çıkarıldı."""
        metrics = ",".join([
            "views", "engagedViews", "likes", "comments", "shares",
            "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
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
            "views", "engagedViews", "likes", "comments", "shares",
            "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
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

    def fetch_channel_breakdown(
        self,
        channel_id: str,
        start_date: str,
        end_date: str,
        metrics: list[str],
        dimensions: list[str],
        filters: list[str] | None = None,
        sort: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[dict]:
        params = {
            "ids": f"channel=={channel_id}",
            "startDate": start_date,
            "endDate": end_date,
            "metrics": ",".join(metrics),
            "dimensions": ",".join(dimensions),
        }
        if filters:
            params["filters"] = ";".join(filters)
        if sort:
            params["sort"] = ",".join(sort)
        if max_results:
            params["maxResults"] = max_results
        data = self._get(f"{ANALYTICS_API_BASE}/reports", params)
        if "error" in data:
            log.debug(f"  Channel breakdown alınamadı ({dimensions}): {data['error']}")
            return []
        headers = [h["name"] for h in data.get("columnHeaders", [])]
        return [dict(zip(headers, row)) for row in data.get("rows", [])]


# ─── Veritabanı işlemleri ──────────────────────────────────
def insert_channel_snapshot(conn: sqlite3.Connection, fetched_at: str, channel: dict):
    conn.execute("""
    INSERT INTO yt_channel_snapshots (
        fetched_at, channel_id, channel_title,
        subscriber_count, view_count, video_count, hidden_subscriber_count,
        thumbnail_url, channel_handle
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        fetched_at,
        channel.get("channel_id"),
        channel.get("channel_title"),
        channel.get("subscriber_count"),
        channel.get("view_count"),
        channel.get("video_count"),
        channel.get("hidden_subscriber_count"),
        channel.get("thumbnail_url"),
        channel.get("channel_handle"),
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
        views, public_views, likes, comments, shares,
        estimated_minutes_watched, average_view_duration, average_view_percentage,
        impressions, impressions_ctr,
        subscribers_gained, subscribers_lost
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        fetched_at, video_id,
        _int("views"), _int("publicViews"), _int("likes"), _int("comments"), _int("shares"),
        _int("estimatedMinutesWatched"), _int("averageViewDuration"),
        _float("averageViewPercentage"),
        _int("impressions"), _float("impressionsClickThroughRate"),
        _int("subscribersGained"), _int("subscribersLost"),
    ))


def merge_video_snapshot_metrics(
    video_item: dict,
    analytics: dict | None,
    public_stats: dict | None = None,
) -> dict:
    """Merge analytics + Data API stats into a single snapshot dict.

    Views priority:
      1. YouTube Data API statistics.viewCount  ← exact same number shown on YouTube UI,
         fetched via fetch_video_details(), no reporting delay.
      2. Analytics API engagedViews             ← has 2-3 day lag, only used as fallback.
      3. Public page scrape publicViews          ← unreliable, last resort.
    """
    analytics = dict(analytics or {})
    stats = (video_item or {}).get("statistics", {}) or {}
    public_stats = dict(public_stats or {})

    # ── Views: Data API is authoritative ──────────────────────────────────────
    if stats.get("viewCount") is not None:
        analytics["views"] = int(stats["viewCount"])   # THIS is what YouTube shows
    else:
        # Fallback: analytics total (may lag by 2-3 days)
        analytics["views"] = analytics.get("engagedViews", analytics.get("views"))

    # Keep scraped value in publicViews for reference / history diff detection
    analytics["publicViews"] = public_stats.get("publicViews")

    # ── Likes: Data API > analytics ───────────────────────────────────────────
    if stats.get("likeCount") is not None:
        analytics["likes"] = int(stats["likeCount"])
    elif public_stats.get("publicLikes") is not None:
        analytics["likes"] = public_stats.get("publicLikes")

    # ── Comments: Data API ────────────────────────────────────────────────────
    if stats.get("commentCount") is not None:
        analytics["comments"] = int(stats["commentCount"])

    return analytics


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


def replace_channel_report_rows(
    conn: sqlite3.Connection,
    fetched_at: str,
    range_start: str,
    range_end: str,
    report_type: str,
    rows: list[dict],
    dimensions: list[str],
):
    conn.execute(
        "DELETE FROM yt_channel_report_rows WHERE fetched_at = ? AND report_type = ?",
        (fetched_at, report_type),
    )
    for row in rows:
        dim_parts = []
        for key in dimensions:
            value = row.get(key)
            if value is not None:
                dim_parts.append(f"{key}={value}")
        dimension_value = " | ".join(dim_parts) if dim_parts else "all"
        for key, value in row.items():
            if key in dimensions:
                continue
            try:
                metric_value = float(value)
            except (TypeError, ValueError):
                continue
            conn.execute("""
            INSERT INTO yt_channel_report_rows (
                fetched_at, range_start, range_end, report_type, dimension, metric_key, metric_value
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fetched_at, report_type, dimension, metric_key) DO UPDATE SET
                metric_value=excluded.metric_value,
                range_start=excluded.range_start,
                range_end=excluded.range_end
            """, (
                fetched_at, range_start, range_end, report_type, dimension_value, key, metric_value
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


def get_known_short_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""
        SELECT video_id
        FROM yt_videos
        WHERE is_short = 1
        ORDER BY published_at DESC
    """).fetchall()
    return [row[0] for row in rows]


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

    analytics_start = (datetime.now(timezone.utc) - timedelta(days=CHANNEL_ANALYTICS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    analytics_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_configs = [
        {
            "report_type": "traffic_source",
            "dimensions": ["insightTrafficSourceType"],
            "metrics": ["views", "estimatedMinutesWatched"],
            "filters": ["creatorContentType==SHORTS"],
            "sort": ["-views"],
            "max_results": 12,
        },
        {
            "report_type": "subscribed_status",
            "dimensions": ["subscribedStatus"],
            "metrics": ["views", "estimatedMinutesWatched"],
            "filters": ["creatorContentType==SHORTS"],
            "sort": ["-views"],
            "max_results": 10,
        },
        {
            "report_type": "device_type",
            "dimensions": ["deviceType"],
            "metrics": ["views", "estimatedMinutesWatched"],
            "filters": ["creatorContentType==SHORTS"],
            "sort": ["-views"],
            "max_results": 10,
        },
        {
            "report_type": "country",
            "dimensions": ["country"],
            "metrics": ["views", "estimatedMinutesWatched"],
            "filters": ["creatorContentType==SHORTS"],
            "sort": ["-views"],
            "max_results": 12,
        },
        {
            "report_type": "retention",
            "dimensions": ["elapsedVideoTimeRatio"],
            "metrics": ["audienceWatchRatio", "relativeRetentionPerformance"],
            "filters": ["creatorContentType==SHORTS"],
            "sort": ["elapsedVideoTimeRatio"],
            "max_results": 20,
        },
    ]
    for config in report_configs:
        rows = fetcher.fetch_channel_breakdown(
            channel_id=channel_id,
            start_date=analytics_start,
            end_date=analytics_end,
            metrics=config["metrics"],
            dimensions=config["dimensions"],
            filters=config.get("filters"),
            sort=config.get("sort"),
            max_results=config.get("max_results"),
        )
        replace_channel_report_rows(
            conn,
            fetched_at,
            analytics_start,
            analytics_end,
            config["report_type"],
            rows,
            config["dimensions"],
        )

    # Video listesi
    published_after = None
    if mode == "hourly":
        cutoff = datetime.now(timezone.utc) - timedelta(days=HOURLY_LOOKBACK_DAYS)
        published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info(f"[yt] Son {HOURLY_LOOKBACK_DAYS} gündeki videolar çekiliyor...")
    else:
        log.info("[yt] Tüm videolar çekiliyor (full mode)...")

    video_ids = fetcher.fetch_channel_videos(channel_id, published_after)
    if mode == "hourly":
        video_ids = list(dict.fromkeys(video_ids + get_known_short_ids(conn)))
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
        # Her zaman videonun yayınlanma tarihini kullan → tüm zamanların toplam izlenmesi
        published = item.get("snippet", {}).get("publishedAt", "2020-01-01")[:10]
        try:
            public_stats = fetcher.fetch_public_video_stats(video_id)

            if mode == "full":
                # Tüm geçmişi günlük granülasyonda çek (yayın tarihinden bugüne)
                daily_rows = fetcher.fetch_video_analytics_daily(
                    video_id, channel_id, published, end_date
                )
                log.debug(f"  {video_id}: {len(daily_rows)} günlük kayıt")
            else:
                # hourly: son HOURLY_LOOKBACK_DAYS günlük veriyi güncelle
                hourly_start = (datetime.now(timezone.utc) - timedelta(days=HOURLY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
                daily_rows = fetcher.fetch_video_analytics_daily(
                    video_id, channel_id, hourly_start, end_date
                )
            for row in daily_rows:
                insert_video_daily(conn, video_id, row)

            # Her iki modda da: LIFETIME aggregate snapshot (published → today)
            # Bu sayede views = tüm zamanların toplam izlenmesi, sadece 14 günlük değil
            analytics = fetcher.fetch_video_analytics(video_id, channel_id, published, end_date)
            snapshot = merge_video_snapshot_metrics(item, analytics, public_stats)
            insert_video_snapshot(conn, fetched_at, video_id, snapshot)
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
