"""
Instagram Graph API Data Fetcher
=================================
Profesyonel Instagram hesabından profil, post, reel, story ve hesap-seviyesi
insights verilerini SQLite veritabanına kaydeder.

Tiered fetching ile çalışır:
- hourly  : profil + son 14 gündeki postlar + aktif storyler          (her saat)
- daily   : eski tüm postlar + account-level insights                  (12 saatte 1)
- weekly  : audience demographics (yaş, cinsiyet, ülke, şehir)         (haftada 1)
- full    : hepsi (ilk kurulum veya manuel tetikleme için)

Kullanım:
    python ig_fetcher.py                    → hourly mode (varsayılan)
    python ig_fetcher.py --mode daily       → eski postlar + account insights
    python ig_fetcher.py --mode weekly      → demographics
    python ig_fetcher.py --mode full        → hepsi
    python ig_fetcher.py --loop             → her saat hourly çalıştır
"""

import os
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
GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"
DB_PATH = os.environ.get("IG_DB_PATH", "instagram_data.db")
TOKEN_FILE = "token.json"
FETCH_INTERVAL_SECONDS = 3600

# Tiered fetching ayarları
HOURLY_LOOKBACK_DAYS = 14       # Son 14 gündeki postlar her saat çekilir
OLD_POSTS_INTERVAL_HOURS = 12   # Eski postlar 12 saatte bir
WEEKLY_INTERVAL_DAYS = 7        # Demographics haftada bir
REQUEST_DELAY_SECONDS = 0.15    # API çağrıları arası küçük gecikme

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ig_fetcher")


# ─── Token Yönetimi ───────────────────────────────────────
class TokenManager:
    """Access token'ı yönetir."""

    def __init__(self, app_id: str, app_secret: str, initial_token: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.token_file = Path(TOKEN_FILE)

        if self.token_file.exists():
            data = json.loads(self.token_file.read_text())
            self.access_token = data["access_token"]
            self.expires_at = data.get("expires_at")
            log.info("Token dosyadan yüklendi.")
        else:
            self.access_token = initial_token
            self.expires_at = time.time() + (55 * 86400)
            self._save()
            log.info("Token kaydedildi (60 gün geçerli).")

    def refresh_if_needed(self):
        if self.expires_at and (self.expires_at - time.time()) < (10 * 86400):
            log.info("Token 10 gün içinde dolacak, yenileniyor...")
            try:
                resp = requests.get(
                    f"{GRAPH_API_BASE}/refresh_access_token",
                    params={
                        "grant_type": "ig_refresh_token",
                        "access_token": self.access_token,
                    },
                    timeout=30,
                )
                data = resp.json()
                if "access_token" in data:
                    self.access_token = data["access_token"]
                    expires_in = data.get("expires_in", 5184000)
                    self.expires_at = time.time() + expires_in
                    self._save()
                    log.info("Token yenilendi.")
                else:
                    log.warning(f"Token yenileme başarısız: {data}")
            except Exception as e:
                log.warning(f"Token yenileme hatası: {e}")

    def _save(self):
        self.token_file.write_text(json.dumps({
            "access_token": self.access_token,
            "expires_at": self.expires_at,
        }, indent=2))

    def get_token(self) -> str:
        self.refresh_if_needed()
        return self.access_token


# ─── Cursor Yönetimi (son çalışma zamanları) ──────────────
def get_cursor(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM fetch_cursors WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_cursor(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO fetch_cursors (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def should_run(conn: sqlite3.Connection, key: str, interval_hours: float) -> bool:
    """Cursor'a bakıp belirli aralıkta çalışıp çalışmayacağını söyler."""
    last_run = get_cursor(conn, key)
    if not last_run:
        return True
    try:
        last_dt = datetime.fromisoformat(last_run)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        return elapsed >= interval_hours
    except Exception:
        return True


# ─── API İstekleri ─────────────────────────────────────────
class InstagramFetcher:

    def __init__(self, token_manager: TokenManager):
        self.token_manager = token_manager
        self.request_count = 0

    def _get(self, url: str, params: dict = None, retries: int = 3) -> dict:
        if params is None:
            params = {}
        params["access_token"] = self.token_manager.get_token()

        for attempt in range(retries):
            try:
                resp = requests.get(url, params=params, timeout=30)
                self.request_count += 1
                if REQUEST_DELAY_SECONDS:
                    time.sleep(REQUEST_DELAY_SECONDS)

                if resp.status_code == 429:
                    wait = 60 * (2 ** attempt)
                    log.warning(f"Rate limit! {wait}s bekleniyor (deneme {attempt + 1}/{retries})...")
                    time.sleep(wait)
                    continue

                data = resp.json()
                if "error" in data:
                    code = data["error"].get("code")
                    if code == 4 or code == 17:  # rate limit kodları
                        wait = 60 * (2 ** attempt)
                        log.warning(f"API rate limit (code={code}). {wait}s bekleniyor...")
                        time.sleep(wait)
                        continue
                return data
            except requests.exceptions.RequestException as e:
                log.warning(f"Network hatası (deneme {attempt + 1}/{retries}): {e}")
                time.sleep(5 * (2 ** attempt))

        return {"error": {"message": "Tüm denemeler başarısız"}}

    # ── Profil ──
    def fetch_profile(self) -> dict:
        fields = ("user_id,username,name,biography,followers_count,follows_count,"
                  "media_count,profile_picture_url,website")
        return self._get(f"{GRAPH_API_BASE}/me", {"fields": fields})

    # ── Medya listesi ──
    def fetch_all_media(self, since_iso: str | None = None) -> list:
        """Tüm medyaları sayfalama ile çeker. since_iso verilirse sadece o tarihten sonrakileri döndürür."""
        all_media = []
        fields = "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url"
        url = f"{GRAPH_API_BASE}/me/media"
        params = {"fields": fields, "limit": 50}

        since_dt = None
        if since_iso:
            try:
                since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
            except ValueError:
                since_dt = None

        while url:
            data = self._get(url, params)
            if "error" in data:
                break

            media_list = data.get("data", [])
            stop = False

            for media in media_list:
                if since_dt:
                    ts = media.get("timestamp")
                    if ts:
                        try:
                            media_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if media_dt < since_dt:
                                stop = True
                                continue
                        except ValueError:
                            pass
                all_media.append(media)

            if stop:
                break

            paging = data.get("paging", {})
            url = paging.get("next")
            params = {}

        log.info(f"  {len(all_media)} medya çekildi.")
        return all_media

    # ── Post insights ──
    def fetch_media_insights(self, media_id: str, media_product_type: str) -> dict:
        """Post veya reel için insights."""

        post_metrics = [
            "reach",
            "saved",
            "shares",
            "likes",
            "comments",
            "views",
            "total_interactions",
            "follows",
            "profile_visits",
            "profile_activity",
        ]

        reels_metrics = [
            "reach",
            "saved",
            "shares",
            "likes",
            "comments",
            "views",
            "total_interactions",
            "ig_reels_avg_watch_time",
            "ig_reels_video_view_total_time",
        ]

        if media_product_type == "REELS":
            metrics = reels_metrics
        else:
            metrics = post_metrics

        data = self._get(
            f"{GRAPH_API_BASE}/{media_id}/insights",
            {"metric": ",".join(metrics)},
        )

        result = {}

        if "error" in data:
            log.warning(
                f" Insights alınamadı ({media_id}, type={media_product_type}): "
                f"{data['error'].get('message', '')}"
            )
            return result

        for item in data.get("data", []):
            name = item["name"]
            value = item["values"][0]["value"] if item.get("values") else 0

            if isinstance(value, dict):
                value = sum(v for v in value.values() if isinstance(v, (int, float)))

            result[name] = value

        return result

    # ── Stories ──
    def fetch_stories(self) -> list:
        fields = "id,media_type,permalink,timestamp,thumbnail_url,media_url"
        data = self._get(f"{GRAPH_API_BASE}/me/stories", {"fields": fields, "limit": 50})
        return data.get("data", []) if "error" not in data else []

    def fetch_story_insights(self, story_id: str) -> dict:
        metrics = "reach,replies,views,total_interactions,navigation"
        data = self._get(
            f"{GRAPH_API_BASE}/{story_id}/insights",
            {"metric": metrics},
        )
        result = {}
        if "error" in data:
            log.warning(f"  Story insights alınamadı ({story_id}): {data['error'].get('message', '')}")
            return result
        for item in data.get("data", []):
            name = item["name"]
            value = item["values"][0]["value"] if item.get("values") else 0
            result[name] = value
        return result

    # ── Account-level insights ──
    def fetch_account_insights(self) -> dict:
        """Hesap-seviyesi günlük insights. Bazı metrikler Creator hesabında kısıtlı olabilir."""
        # Tek tek dene; biri patlarsa diğerleri etkilenmesin
        metrics_day = [
            "reach", "profile_views", "accounts_engaged", "total_interactions",
            "website_clicks", "follower_count",
        ]
        result = {}
        for metric in metrics_day:
            data = self._get(
                f"{GRAPH_API_BASE}/me/insights",
                {"metric": metric, "period": "day"},
            )
            if "error" in data:
                log.debug(f"  Account insight '{metric}' alınamadı: {data['error'].get('message', '')}")
                continue
            for item in data.get("data", []):
                values = item.get("values", [])
                if values:
                    v = values[-1].get("value", 0)  # son günün değeri
                    if isinstance(v, dict):
                        v = sum(x for x in v.values() if isinstance(x, (int, float)))
                    result[metric] = v
        return result

    def fetch_online_followers(self) -> dict:
        """Saat saat takipçinin online olduğu zaman dilimi. period=lifetime."""
        data = self._get(
            f"{GRAPH_API_BASE}/me/insights",
            {"metric": "online_followers", "period": "lifetime"},
        )
        if "error" in data:
            log.debug(f"  online_followers alınamadı: {data['error'].get('message', '')}")
            return {}
        result = {}
        for item in data.get("data", []):
            values = item.get("values", [])
            for v in values:
                end_time = v.get("end_time", "")
                value_obj = v.get("value", {})
                if isinstance(value_obj, dict):
                    result[end_time] = value_obj
        return result

    # ── Follower history (backfill) ──
    def fetch_follower_history(self, days: int = 90) -> list:
        """Son `days` günlük günlük follower_count delta verisini çeker.
        Her eleman: {"date": "YYYY-MM-DD", "delta": int}
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)
        until = datetime.now(timezone.utc)
        data = self._get(
            f"{GRAPH_API_BASE}/me/insights",
            {
                "metric": "follower_count",
                "period": "day",
                "since": int(since.timestamp()),
                "until": int(until.timestamp()),
            },
        )
        if "error" in data:
            log.warning(f"  follower_count history alınamadı: {data['error'].get('message', '')}")
            return []

        results = []
        seen_dates = set()
        for item in data.get("data", []):
            if item.get("name") != "follower_count":
                continue
            for v in item.get("values", []):
                end_time = v.get("end_time", "")
                date_str = end_time[:10] if end_time else None
                value = v.get("value", 0)
                if date_str and date_str not in seen_dates:
                    results.append({"date": date_str, "delta": value})
                    seen_dates.add(date_str)

        # Sayfalama (insights endpoint'i nadiren sayfalıyor ama yine de handle et)
        next_url = data.get("paging", {}).get("next")
        while next_url:
            page = self._get(next_url)
            if "error" in page:
                break
            for item in page.get("data", []):
                if item.get("name") != "follower_count":
                    continue
                for v in item.get("values", []):
                    end_time = v.get("end_time", "")
                    date_str = end_time[:10] if end_time else None
                    value = v.get("value", 0)
                    if date_str and date_str not in seen_dates:
                        results.append({"date": date_str, "delta": value})
                        seen_dates.add(date_str)
            next_url = page.get("paging", {}).get("next")

        results.sort(key=lambda x: x["date"])
        log.info(f"  Follower history: {len(results)} gün alındı.")
        return results

    def _merge_follow_unfollow_value(self, daily: dict, date_str: str | None, value):
        if not date_str:
            return
        row = daily.setdefault(date_str, {"date": date_str, "follows": None, "unfollows": None})
        if isinstance(value, (int, float)):
            row["follows"] = int(value)
            return
        if not isinstance(value, dict):
            return

        direct_follow_keys = ("follows", "follow", "followers", "new_followers", "follower")
        direct_unfollow_keys = ("unfollows", "unfollow", "unfollowers", "lost_followers", "nonfollower")
        for key, raw in value.items():
            key_norm = str(key).lower()
            if not isinstance(raw, (int, float)):
                continue
            if any(part in key_norm for part in direct_unfollow_keys):
                row["unfollows"] = int(raw)
            elif any(part in key_norm for part in direct_follow_keys):
                row["follows"] = int(raw)

    def _merge_follow_unfollow_breakdowns(self, daily: dict, end_time: str | None, breakdowns: list):
        date_str = end_time[:10] if end_time else None
        if not date_str:
            return
        for breakdown in breakdowns or []:
            for result in breakdown.get("results", []):
                value = result.get("value")
                labels = result.get("dimension_values") or []
                label = " ".join(str(x).lower() for x in labels)
                row = daily.setdefault(date_str, {"date": date_str, "follows": None, "unfollows": None})
                if not isinstance(value, (int, float)):
                    continue
                if "unfollow" in label or "nonfollower" in label:
                    row["unfollows"] = int(value)
                elif "follow" in label or "follower" in label:
                    row["follows"] = int(value)

    def _extract_follow_unfollow_history(self, data: dict) -> list:
        daily = {}
        for item in data.get("data", []):
            if item.get("name") != "follows_and_unfollows":
                continue
            for v in item.get("values", []):
                end_time = v.get("end_time", "")
                self._merge_follow_unfollow_value(daily, end_time[:10] if end_time else None, v.get("value"))

            total_value = item.get("total_value")
            if isinstance(total_value, dict):
                end_time = item.get("period_end_time") or item.get("end_time")
                self._merge_follow_unfollow_value(daily, end_time[:10] if end_time else None, total_value.get("value"))
                self._merge_follow_unfollow_breakdowns(daily, end_time, total_value.get("breakdowns", []))

        return sorted((row for row in daily.values() if row.get("date")), key=lambda x: x["date"])

    def fetch_follow_unfollow_history(self, days: int = 90) -> list:
        """follows_and_unfollows metriğini deneyip günlük follow/unfollow ayrımı döndürür."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        until = datetime.now(timezone.utc)
        attempts = [
            {"metric": "follows_and_unfollows", "period": "day", "metric_type": "total_value",
             "breakdown": "follow_type"},
            {"metric": "follows_and_unfollows", "period": "day", "metric_type": "total_value"},
            {"metric": "follows_and_unfollows", "period": "day"},
        ]

        for params in attempts:
            request_params = {
                **params,
                "since": int(since.timestamp()),
                "until": int(until.timestamp()),
            }
            data = self._get(f"{GRAPH_API_BASE}/me/insights", request_params)
            if "error" in data:
                log.info(f"  follows_and_unfollows denenemedi ({params}): "
                         f"{data['error'].get('message', '')}")
                continue

            results = self._extract_follow_unfollow_history(data)
            if results:
                log.info(f"  follows_and_unfollows: {len(results)} gün alındı.")
                return results

            log.info(f"  follows_and_unfollows cevap verdi ama günlük ayrım parse edilemedi ({params}).")

        return []

    # ── Demographics ──
    def fetch_demographics(self) -> dict:
        """Takipçi demografisi. Creator hesabında bazıları çalışmayabilir."""
        breakdowns = ["age", "gender", "country", "city"]
        result = {}
        for breakdown in breakdowns:
            data = self._get(
                f"{GRAPH_API_BASE}/me/insights",
                {
                    "metric": "engaged_audience_demographics",  # follower_demographics deprecated → engaged_audience_demographics
                    "period": "lifetime",
                    "timeframe": "this_month",
                    "breakdown": breakdown,
                    "metric_type": "total_value",
                },
            )
            if "error" in data:
                # Fallback olarak follower_demographics dene (eski API'lerde)
                data = self._get(
                    f"{GRAPH_API_BASE}/me/insights",
                    {
                        "metric": "follower_demographics",
                        "period": "lifetime",
                        "timeframe": "this_month",
                        "breakdown": breakdown,
                        "metric_type": "total_value",
                    },
                )

            if "error" in data:
                log.info(f"  Demographics '{breakdown}' alınamadı: {data['error'].get('message', '')}")
                continue

            for item in data.get("data", []):
                total = item.get("total_value", {})
                breakdowns_arr = total.get("breakdowns", [])
                for bd in breakdowns_arr:
                    results = bd.get("results", [])
                    for r in results:
                        dim_value = r.get("dimension_values", [])
                        v = r.get("value", 0)
                        if dim_value:
                            result.setdefault(breakdown, {})[dim_value[0]] = v
        return result


# ─── Veritabanı işlemleri ─────────────────────────────────
def insert_profile(conn: sqlite3.Connection, fetched_at: str, profile: dict):
    conn.execute("""
    INSERT INTO profile_snapshots (
        fetched_at, user_id, username, name, biography,
        followers_count, follows_count, media_count,
        profile_picture_url, website
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        fetched_at,
        profile.get("user_id"),
        profile.get("username"),
        profile.get("name"),
        profile.get("biography"),
        profile.get("followers_count"),
        profile.get("follows_count"),
        profile.get("media_count"),
        profile.get("profile_picture_url"),
        profile.get("website"),
    ))


def upsert_post(conn: sqlite3.Connection, media: dict):
    permalink = media.get("permalink") or ""
    shortcode = permalink.split("/")[-2] if permalink else None
    conn.execute("""
    INSERT INTO posts (post_id, shortcode, media_type, media_product_type,
                      permalink, caption, timestamp, thumbnail_url)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(post_id) DO UPDATE SET
        media_type=excluded.media_type,
        media_product_type=excluded.media_product_type,
        permalink=excluded.permalink,
        caption=excluded.caption,
        thumbnail_url=excluded.thumbnail_url
    """, (
        media["id"],
        shortcode,
        media.get("media_type", ""),
        media.get("media_product_type", ""),
        media.get("permalink"),
        media.get("caption"),
        media.get("timestamp"),
        media.get("thumbnail_url"),
    ))


def insert_post_snapshot(conn: sqlite3.Connection, fetched_at: str, post_id: str, insights: dict):
    conn.execute("""
    INSERT INTO post_snapshots (
        fetched_at, post_id, like_count, comments_count,
        reach, saved, shares, views, total_interactions,
        follows, profile_visits, profile_activity,
        ig_reels_avg_watch_time, ig_reels_video_view_total_time,
        clips_replays_count, ig_reels_aggregated_all_plays_count
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        fetched_at, post_id,
        insights.get("likes", 0),
        insights.get("comments", 0),
        insights.get("reach"),
        insights.get("saved"),
        insights.get("shares"),
        insights.get("views"),
        insights.get("total_interactions"),
        insights.get("follows"),
        insights.get("profile_visits"),
        insights.get("profile_activity"),
        insights.get("ig_reels_avg_watch_time"),
        insights.get("ig_reels_video_view_total_time"),
        insights.get("clips_replays_count"),
        insights.get("ig_reels_aggregated_all_plays_count"),
    ))


def upsert_story(conn: sqlite3.Connection, story: dict, first_seen: str):
    conn.execute("""
    INSERT INTO stories (story_id, media_type, permalink, timestamp,
                        thumbnail_url, media_url, first_seen_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(story_id) DO UPDATE SET
        media_type=excluded.media_type,
        permalink=excluded.permalink,
        thumbnail_url=excluded.thumbnail_url,
        media_url=excluded.media_url
    """, (
        story["id"],
        story.get("media_type"),
        story.get("permalink"),
        story.get("timestamp"),
        story.get("thumbnail_url"),
        story.get("media_url"),
        first_seen,
    ))


def insert_story_snapshot(conn: sqlite3.Connection, fetched_at: str, story_id: str, insights: dict):
    conn.execute("""
        INSERT INTO story_snapshots (
            fetched_at, story_id,
            reach, replies, views, total_interactions, navigation
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        fetched_at,
        story_id,
        insights.get("reach"),
        insights.get("replies"),
        insights.get("views"),
        insights.get("total_interactions"),
        insights.get("navigation"),
    ))


def insert_account_insights(conn: sqlite3.Connection, fetched_at: str, period_date: str, data: dict):
    conn.execute("""
    INSERT INTO account_insights (
        fetched_at, period_date, reach, impressions, profile_views,
        accounts_engaged, total_interactions, follower_count, website_clicks,
        email_contacts, phone_call_clicks, text_message_clicks, get_directions_clicks
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(period_date) DO UPDATE SET
        fetched_at=excluded.fetched_at,
        reach=COALESCE(excluded.reach, account_insights.reach),
        profile_views=COALESCE(excluded.profile_views, account_insights.profile_views),
        accounts_engaged=COALESCE(excluded.accounts_engaged, account_insights.accounts_engaged),
        total_interactions=COALESCE(excluded.total_interactions, account_insights.total_interactions),
        follower_count=COALESCE(excluded.follower_count, account_insights.follower_count),
        website_clicks=COALESCE(excluded.website_clicks, account_insights.website_clicks)
    """, (
        fetched_at, period_date,
        data.get("reach"),
        data.get("impressions"),
        data.get("profile_views"),
        data.get("accounts_engaged"),
        data.get("total_interactions"),
        data.get("follower_count"),
        data.get("website_clicks"),
        data.get("email_contacts"),
        data.get("phone_call_clicks"),
        data.get("text_message_clicks"),
        data.get("get_directions_clicks"),
    ))


def insert_demographics(conn: sqlite3.Connection, fetched_at: str, demographics: dict):
    for breakdown, values in demographics.items():
        for dimension, value in values.items():
            conn.execute("""
            INSERT INTO audience_demographics (fetched_at, breakdown, dimension, value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fetched_at, breakdown, dimension) DO UPDATE SET value = excluded.value
            """, (fetched_at, breakdown, dimension, value))


def upsert_follower_daily(conn: sqlite3.Connection, date: str, followers_count: int,
                          followers_delta: int | None, source: str,
                          follows: int | None = None, unfollows: int | None = None):
    conn.execute("""
    INSERT INTO follower_daily (date, followers_count, followers_delta, follows, unfollows, source)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(date) DO UPDATE SET
        followers_count = CASE
            WHEN excluded.source = 'snapshot' THEN excluded.followers_count
            WHEN follower_daily.source = 'snapshot' THEN follower_daily.followers_count
            ELSE excluded.followers_count
        END,
        followers_delta = COALESCE(excluded.followers_delta, follower_daily.followers_delta),
        follows = COALESCE(excluded.follows, follower_daily.follows),
        unfollows = COALESCE(excluded.unfollows, follower_daily.unfollows),
        source = CASE
            WHEN excluded.source = 'snapshot' OR follower_daily.source = 'snapshot' THEN 'snapshot'
            ELSE excluded.source
        END
    """, (date, followers_count, followers_delta, follows, unfollows, source))


def insert_online_followers(conn: sqlite3.Connection, fetched_at: str, online: dict):
    for end_time, hours_dict in online.items():
        period_date = end_time.split("T")[0] if end_time else fetched_at[:10]
        for hour_str, value in hours_dict.items():
            try:
                hour = int(hour_str)
            except ValueError:
                continue
            conn.execute("""
            INSERT INTO online_followers (fetched_at, period_date, hour, value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(period_date, hour) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                value = excluded.value
            """, (fetched_at, period_date, hour, value))


def log_fetch_run(conn: sqlite3.Connection, started_at: str, completed_at: str,
                  mode: str, status: str, posts: int, stories: int,
                  requests_count: int, error: str | None = None):
    conn.execute("""
    INSERT INTO fetch_runs (started_at, completed_at, mode, status,
                           posts_fetched, stories_fetched, api_requests, error_message)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (started_at, completed_at, mode, status, posts, stories, requests_count, error))


# ─── Ana Çalışma Döngüleri ────────────────────────────────
def run_hourly(fetcher: InstagramFetcher, conn: sqlite3.Connection, fetched_at: str) -> tuple[int, int]:
    """Profil + son 14 gündeki postlar + aktif storyler."""
    posts_count, stories_count = 0, 0

    # Profil
    log.info("[hourly] Profil bilgileri...")
    profile = fetcher.fetch_profile()
    if "error" not in profile:
        insert_profile(conn, fetched_at, profile)
        today_str = fetched_at[:10]
        upsert_follower_daily(conn, today_str, profile.get("followers_count"), None, "snapshot")
        log.info(f"  @{profile.get('username')} | Takipçi: {profile.get('followers_count')} | "
                 f"Post: {profile.get('media_count')}")

    # Son 14 gündeki postlar
    since_dt = datetime.now(timezone.utc) - timedelta(days=HOURLY_LOOKBACK_DAYS)
    since_iso = since_dt.isoformat()
    log.info(f"[hourly] Son {HOURLY_LOOKBACK_DAYS} gündeki postlar çekiliyor (since={since_iso[:10]})...")
    recent_media = fetcher.fetch_all_media(since_iso=since_iso)

    for i, media in enumerate(recent_media, 1):
        try:
            upsert_post(conn, media)
            insights = fetcher.fetch_media_insights(media["id"], media.get("media_product_type", ""))
            insert_post_snapshot(conn, fetched_at, media["id"], insights)
            posts_count += 1
        except Exception as e:
            log.warning(f"  Post atlandı ({media.get('id')}): {e}")
        if i % 10 == 0:
            log.info(f"  İlerleme: {i}/{len(recent_media)} | API: {fetcher.request_count}")

    # Storyler
    log.info("[hourly] Aktif storyler çekiliyor...")
    stories = fetcher.fetch_stories()
    for story in stories:
        try:
            upsert_story(conn, story, fetched_at)
            insights = fetcher.fetch_story_insights(story["id"])
            insert_story_snapshot(conn, fetched_at, story["id"], insights)
            stories_count += 1
        except Exception as e:
            log.warning(f"  Story atlandı ({story.get('id')}): {e}")

    log.info(f"[hourly] Bitti: {posts_count} post, {stories_count} story.")
    return posts_count, stories_count


def run_old_posts(fetcher: InstagramFetcher, conn: sqlite3.Connection, fetched_at: str) -> int:
    """Tüm eski postları (14 günden daha eski) çek. 12 saatte 1 çalışır."""
    log.info("[old_posts] Tüm eski postlar çekiliyor...")
    cutoff = datetime.now(timezone.utc) - timedelta(days=HOURLY_LOOKBACK_DAYS)
    all_media = fetcher.fetch_all_media()

    old_media = []
    for media in all_media:
        ts = media.get("timestamp")
        if not ts:
            continue
        try:
            media_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if media_dt < cutoff:
                old_media.append(media)
        except ValueError:
            continue

    count = 0
    log.info(f"[old_posts] {len(old_media)} eski post için insights çekiliyor...")
    for i, media in enumerate(old_media, 1):
        try:
            upsert_post(conn, media)
            insights = fetcher.fetch_media_insights(media["id"], media.get("media_product_type", ""))
            insert_post_snapshot(conn, fetched_at, media["id"], insights)
            count += 1
        except Exception as e:
            log.warning(f"  Post atlandı ({media.get('id')}): {e}")
        if i % 25 == 0:
            log.info(f"  İlerleme: {i}/{len(old_media)} | API: {fetcher.request_count}")

    set_cursor(conn, "last_old_posts_fetch", fetched_at)
    log.info(f"[old_posts] Bitti: {count} eski post.")
    return count


def run_account_insights(fetcher: InstagramFetcher, conn: sqlite3.Connection, fetched_at: str):
    log.info("[account] Hesap-seviyesi insights çekiliyor...")
    insights = fetcher.fetch_account_insights()
    if insights:
        period_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        insert_account_insights(conn, fetched_at, period_date, insights)
        log.info(f"  Kayıt: {insights}")

    log.info("[account] online_followers çekiliyor...")
    online = fetcher.fetch_online_followers()
    if online:
        insert_online_followers(conn, fetched_at, online)
        log.info(f"  {sum(len(v) for v in online.values())} saatlik kayıt.")

    set_cursor(conn, "last_account_insights_fetch", fetched_at)


def run_backfill_followers(fetcher: InstagramFetcher, conn: sqlite3.Connection, days: int = 90):
    """API'den days günlük follower delta çekip follower_daily tablosunu doldurur.

    Mantık:
    - profile_snapshots'taki mevcut günlük değerler her zaman önceliklidir (source='snapshot').
    - Daha eski günler için: en eski snapshot'tan (10 Mayıs) geriye doğru delta ile hesaplanır.
    - Ayrıca 10 Mayıs'tan bugüne kadar snapshot değerleri de follower_daily'ye aktarılır.
    """
    log.info(f"[backfill] {days} günlük follower history başlatılıyor...")

    # 1. Mevcut snapshot'lardan günlük son değerleri al
    cur = conn.execute("""
        SELECT DATE(fetched_at) AS d, followers_count
        FROM profile_snapshots
        WHERE followers_count IS NOT NULL
        ORDER BY fetched_at
    """)
    snapshots_by_day: dict[str, int] = {}
    for row in cur.fetchall():
        snapshots_by_day[row[0]] = row[1]  # aynı günden birden fazlaysa son kazanır

    log.info(f"  {len(snapshots_by_day)} günlük snapshot bulundu: "
             f"{min(snapshots_by_day) if snapshots_by_day else '-'} - "
             f"{max(snapshots_by_day) if snapshots_by_day else '-'}")

    # 2. API'den takip / bırakma verisini çek. Bu gelirse net değişim daha güvenilir olur.
    follow_unfollow_list = fetcher.fetch_follow_unfollow_history(days)
    follow_unfollow_by_date = {d["date"]: d for d in follow_unfollow_list}
    net_delta_dict: dict[str, int] = {}
    for date_str, row in follow_unfollow_by_date.items():
        follows = row.get("follows")
        unfollows = row.get("unfollows")
        if follows is not None and unfollows is not None:
            net_delta_dict[date_str] = follows - unfollows

    if net_delta_dict:
        total_follows = sum((row.get("follows") or 0) for row in follow_unfollow_by_date.values())
        total_unfollows = sum((row.get("unfollows") or 0) for row in follow_unfollow_by_date.values())
        log.info(f"  follows_and_unfollows net kullanılacak: "
                 f"{total_follows} follows, {total_unfollows} unfollows.")

    # Eski follower_count metriği yedek olarak tutulur.
    deltas_list = fetcher.fetch_follower_history(days)
    fallback_delta_dict: dict[str, int] = {d["date"]: d["delta"] for d in deltas_list}
    if not net_delta_dict:
        net_delta_dict = fallback_delta_dict
        if net_delta_dict:
            log.warning("  follows_and_unfollows günlük net vermedi; follower_count metriğiyle devam ediliyor.")

    if not net_delta_dict:
        # API'den veri gelmedi; sadece snapshot'ları aktar
        log.warning("[backfill] API'den delta alınamadı, yalnızca snapshot verisi kaydediliyor.")
        for date_str, count in snapshots_by_day.items():
            upsert_follower_daily(conn, date_str, count, None, "snapshot")
        conn.commit()
        return

    log.info(f"  API net delta aralığı: {min(net_delta_dict)} - {max(net_delta_dict)}")

    # 3. Referans noktası: en güncel snapshot (bugün veya dünün gerçek sayısı)
    latest_snapshot_date = max(snapshots_by_day)
    reference_count = snapshots_by_day[latest_snapshot_date]
    log.info(f"  Referans: {latest_snapshot_date} = {reference_count} takipçi")

    # 4. Referans gününden API'nin en eski gününe kadar geriye doğru mutlak sayıları hesapla
    #    count[d-1] = count[d] - delta[d]
    from datetime import date as date_cls
    ref_date = date_cls.fromisoformat(latest_snapshot_date)
    earliest = date_cls.fromisoformat(min(net_delta_dict))

    reconstructed: dict[str, int] = {}
    current_count = reference_count
    current_date = ref_date

    while current_date >= earliest:
        date_str = str(current_date)
        reconstructed[date_str] = current_count
        delta = net_delta_dict.get(date_str, 0)
        current_count -= delta
        current_date -= timedelta(days=1)

    # 5. DB'ye yaz — snapshot günleri öncelikli
    saved_snap, saved_api = 0, 0
    all_dates = set(reconstructed) | set(snapshots_by_day)
    for date_str in sorted(all_dates):
        delta = net_delta_dict.get(date_str)
        activity = follow_unfollow_by_date.get(date_str, {})
        follows = activity.get("follows")
        unfollows = activity.get("unfollows")
        if date_str in snapshots_by_day:
            upsert_follower_daily(conn, date_str, snapshots_by_day[date_str], delta, "snapshot",
                                  follows, unfollows)
            saved_snap += 1
        elif date_str in reconstructed:
            upsert_follower_daily(conn, date_str, reconstructed[date_str], delta, "api_backfill",
                                  follows, unfollows)
            saved_api += 1

    conn.commit()
    log.info(f"[backfill] Tamamlandı: {saved_snap} snapshot + {saved_api} api_backfill günü kaydedildi.")


def run_demographics(fetcher: InstagramFetcher, conn: sqlite3.Connection, fetched_at: str):
    log.info("[demographics] Audience demographics çekiliyor...")
    demo = fetcher.fetch_demographics()
    if demo:
        insert_demographics(conn, fetched_at, demo)
        log.info(f"  Breakdown'lar: {list(demo.keys())}")
    set_cursor(conn, "last_demographics_fetch", fetched_at)


# ─── Mod Çalıştırıcı ──────────────────────────────────────
def execute_mode(fetcher: InstagramFetcher, conn: sqlite3.Connection, mode: str):
    """Modu kontrol edip ilgili runner'ları çağırır.

    auto modu:
      - hourly her zaman çalışır
      - eski postlar son 12 saatten eski ise çalışır
      - account insights son 12 saatten eski ise çalışır
      - demographics son 7 günden eski ise çalışır
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    fetcher.request_count = 0
    posts, stories = 0, 0
    error = None

    try:
        if mode in ("hourly", "auto", "full"):
            p, s = run_hourly(fetcher, conn, fetched_at)
            posts += p
            stories += s

        if mode == "full" or (mode == "auto" and should_run(conn, "last_old_posts_fetch", OLD_POSTS_INTERVAL_HOURS)) or mode == "daily":
            p = run_old_posts(fetcher, conn, fetched_at)
            posts += p

        if mode == "full" or (mode == "auto" and should_run(conn, "last_account_insights_fetch", OLD_POSTS_INTERVAL_HOURS)) or mode == "daily":
            run_account_insights(fetcher, conn, fetched_at)

        if mode == "full" or (mode == "auto" and should_run(conn, "last_demographics_fetch", WEEKLY_INTERVAL_DAYS * 24)) or mode == "weekly":
            run_demographics(fetcher, conn, fetched_at)

        if mode == "backfill":
            run_backfill_followers(fetcher, conn)

        status = "success"
    except Exception as e:
        error = str(e)
        status = "error"
        log.exception(f"Hata: {e}")

    completed_at = datetime.now(timezone.utc).isoformat()
    log_fetch_run(conn, fetched_at, completed_at, mode, status, posts, stories,
                  fetcher.request_count, error)
    conn.commit()
    log.info(f"Mod '{mode}' bitti. Posts: {posts}, Stories: {stories}, "
             f"API: {fetcher.request_count}, Status: {status}")


def main():
    parser = argparse.ArgumentParser(description="Instagram Graph API Data Fetcher")
    parser.add_argument("--mode", default="auto",
                        choices=["auto", "hourly", "daily", "weekly", "full", "backfill"],
                        help="Çalışma modu. backfill = 90 günlük follower geçmişini doldur")
    parser.add_argument("--loop", action="store_true", help="Sürekli çalış (saatte bir)")
    parser.add_argument("--interval", type=int, default=FETCH_INTERVAL_SECONDS,
                        help="Loop modunda çalışma aralığı (saniye)")
    args = parser.parse_args()

    app_id = os.environ.get("IG_APP_ID")
    app_secret = os.environ.get("IG_APP_SECRET")
    access_token = os.environ.get("IG_ACCESS_TOKEN")

    if not all([app_id, app_secret, access_token]):
        print("""
╔══════════════════════════════════════════════════════════════╗
║  .env dosyası oluştur veya ortam değişkenlerini ayarla:     ║
║    IG_APP_ID, IG_APP_SECRET, IG_ACCESS_TOKEN                ║
╚══════════════════════════════════════════════════════════════╝
        """)
        sys.exit(1)

    # Migration tablolarının varlığını garanti et
    try:
        from migrate_schema import migrate
        conn = sqlite3.connect(DB_PATH)
        migrate(conn)
    except ImportError:
        conn = sqlite3.connect(DB_PATH)
        log.warning("migrate_schema.py bulunamadı; tabloların manuel oluşturulduğu varsayılıyor.")

    token_manager = TokenManager(app_id, app_secret, access_token)
    fetcher = InstagramFetcher(token_manager)

    if args.loop:
        log.info(f"Döngü modu: her {args.interval}s, mode={args.mode}")
        while True:
            try:
                execute_mode(fetcher, conn, args.mode)
            except Exception as e:
                log.error(f"Hata: {e}")
            log.info(f"Sonraki çalışma: {args.interval}s sonra...")
            time.sleep(args.interval)
    else:
        execute_mode(fetcher, conn, args.mode)

    conn.close()


if __name__ == "__main__":
    main()
