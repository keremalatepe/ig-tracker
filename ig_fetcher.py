"""
Instagram Graph API Data Fetcher
=================================
Profesyonel Instagram hesabından tüm post/reel verilerini ve insights'ları
saatte bir çekip SQLite veritabanına kaydeder.

Kullanım:
  1. .env dosyasını doldur (IG_APP_ID, IG_APP_SECRET, IG_ACCESS_TOKEN)
  2. pip install requests python-dotenv
  3. İlk çalıştırmada token'ı uzun süreli token'a çevirir
  4. python ig_fetcher.py          → tek seferlik çalıştır
  5. python ig_fetcher.py --loop   → saatte bir otomatik çalıştır
"""

import os
import sys
import json
import time
import sqlite3
import logging
import argparse
from datetime import datetime, timezone
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
    pass  # .env kullanılmıyorsa sorun değil

# ─── Ayarlar ───────────────────────────────────────────────
GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"
DB_PATH = os.environ.get("IG_DB_PATH", "instagram_data.db")
TOKEN_FILE = "token.json"
FETCH_INTERVAL_SECONDS = 3600  # 1 saat

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("ig_fetcher")


# ─── Token Yönetimi ───────────────────────────────────────
class TokenManager:
    """Access token'ı yönetir: uzun süreli token'a çevirir ve yeniler."""

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
            self.expires_at = None
            self._exchange_for_long_lived()

    def _exchange_for_long_lived(self):
        """Kısa süreli token'ı uzun süreli (60 gün) token'a çevirir."""
        log.info("Token uzun süreli token'a çevriliyor...")
        resp = requests.get(
            f"{GRAPH_API_BASE}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": self.app_secret,
                "access_token": self.access_token,
            }
        )
        data = resp.json()
        if "access_token" in data:
            self.access_token = data["access_token"]
            expires_in = data.get("expires_in", 5184000)
            self.expires_at = time.time() + expires_in
            self._save()
            log.info(f"Uzun süreli token alındı. {expires_in // 86400} gün geçerli.")
        else:
            log.error(f"Token exchange hatası: {data}")
            raise Exception(f"Token exchange başarısız: {data}")

    def refresh_if_needed(self):
        """Token 50 günden eskiyse yeniler."""
        if self.expires_at and (self.expires_at - time.time()) < (10 * 86400):
            log.info("Token 10 gün içinde dolacak, yenileniyor...")
            resp = requests.get(
                f"{GRAPH_API_BASE}/refresh_access_token",
                params={
                    "grant_type": "ig_refresh_token",
                    "access_token": self.access_token,
                }
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

    def _save(self):
        self.token_file.write_text(json.dumps({
            "access_token": self.access_token,
            "expires_at": self.expires_at,
        }, indent=2))

    def get_token(self) -> str:
        self.refresh_if_needed()
        return self.access_token


# ─── Veritabanı ────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS profile_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        user_id TEXT,
        username TEXT,
        name TEXT,
        biography TEXT,
        followers_count INTEGER,
        follows_count INTEGER,
        media_count INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        post_id TEXT PRIMARY KEY,
        shortcode TEXT,
        media_type TEXT,
        media_product_type TEXT,
        permalink TEXT,
        caption TEXT,
        timestamp TEXT,
        thumbnail_url TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS post_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL,
        post_id TEXT NOT NULL,
        like_count INTEGER,
        comments_count INTEGER,
        impressions INTEGER,
        reach INTEGER,
        saved INTEGER,
        shares INTEGER,
        plays INTEGER,
        total_interactions INTEGER,
        FOREIGN KEY(post_id) REFERENCES posts(post_id)
    )
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_snapshots_post_id ON post_snapshots(post_id)
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON post_snapshots(fetched_at)
    """)

    conn.commit()
    return conn


# ─── API İstekleri ─────────────────────────────────────────
class InstagramFetcher:

    def __init__(self, token_manager: TokenManager):
        self.token_manager = token_manager
        self.request_count = 0

    def _get(self, url: str, params: dict = None) -> dict:
        if params is None:
            params = {}
        params["access_token"] = self.token_manager.get_token()
        resp = requests.get(url, params=params)
        self.request_count += 1

        if resp.status_code == 429:
            log.warning("Rate limit! 5 dakika bekleniyor...")
            time.sleep(300)
            return self._get(url, params)

        data = resp.json()
        if "error" in data:
            log.error(f"API Hatası: {data['error']}")
        return data

    def fetch_profile(self) -> dict:
        """Profil bilgilerini çeker."""
        fields = "user_id,username,name,biography,followers_count,follows_count,media_count"
        data = self._get(f"{GRAPH_API_BASE}/me", {"fields": fields})
        return data

    def fetch_all_media(self) -> list:
        """Tüm medyaları sayfalama ile çeker."""
        all_media = []
        fields = "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url"
        url = f"{GRAPH_API_BASE}/me/media"
        params = {"fields": fields, "limit": 50}

        while url:
            data = self._get(url, params)
            media_list = data.get("data", [])
            all_media.extend(media_list)
            log.info(f"  {len(all_media)} medya çekildi...")

            paging = data.get("paging", {})
            url = paging.get("next")
            params = {}  # next URL parametreleri içerir

        return all_media

    def fetch_media_insights(self, media_id: str, media_type: str, media_product_type: str) -> dict:
        """Tek bir medya için insights çeker."""
        # Reels ve diğer medya tipleri farklı metrikler destekliyor
        if media_product_type == "REELS":
            metrics = "impressions,reach,saved,shares,plays,total_interactions,likes,comments"
        elif media_type == "VIDEO":
            metrics = "impressions,reach,saved,shares,plays,total_interactions,likes,comments"
        elif media_type == "CAROUSEL_ALBUM":
            metrics = "impressions,reach,saved,shares,total_interactions,likes,comments"
        else:  # IMAGE
            metrics = "impressions,reach,saved,shares,total_interactions,likes,comments"

        data = self._get(
            f"{GRAPH_API_BASE}/{media_id}/insights",
            {"metric": metrics}
        )
        result = {}
        for item in data.get("data", []):
            name = item["name"]
            value = item["values"][0]["value"] if item.get("values") else 0
            result[name] = value

        return result


# ─── Ana Çalışma Döngüsü ──────────────────────────────────
def run_fetch_cycle(fetcher: InstagramFetcher, conn: sqlite3.Connection):
    """Bir tam veri çekme döngüsü."""
    fetched_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    fetcher.request_count = 0

    # 1. Profil bilgileri
    log.info("Profil bilgileri çekiliyor...")
    profile = fetcher.fetch_profile()
    if "error" not in profile:
        cur.execute("""
        INSERT INTO profile_snapshots (fetched_at, user_id, username, name, biography,
                                       followers_count, follows_count, media_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fetched_at,
            profile.get("user_id"),
            profile.get("username"),
            profile.get("name"),
            profile.get("biography"),
            profile.get("followers_count"),
            profile.get("follows_count"),
            profile.get("media_count"),
        ))
        log.info(f"Profil: @{profile.get('username')} | "
                 f"Takipçi: {profile.get('followers_count')} | "
                 f"Post: {profile.get('media_count')}")

    # 2. Tüm medyaları çek
    log.info("Medyalar çekiliyor...")
    all_media = fetcher.fetch_all_media()

    # 3. Her medya için kaydet + insights çek
    log.info(f"Toplam {len(all_media)} medya için insights çekiliyor...")
    for i, media in enumerate(all_media, 1):
        post_id = media["id"]
        media_type = media.get("media_type", "")
        media_product_type = media.get("media_product_type", "")

        # Post bilgilerini kaydet/güncelle
        cur.execute("""
        INSERT INTO posts (post_id, shortcode, media_type, media_product_type,
                          permalink, caption, timestamp, thumbnail_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            media_type=excluded.media_type,
            media_product_type=excluded.media_product_type,
            permalink=excluded.permalink,
            caption=excluded.caption
        """, (
            post_id,
            media.get("permalink", "").split("/")[-2] if media.get("permalink") else None,
            media_type,
            media_product_type,
            media.get("permalink"),
            media.get("caption"),
            media.get("timestamp"),
            media.get("thumbnail_url"),
        ))

        # Insights çek
        insights = fetcher.fetch_media_insights(post_id, media_type, media_product_type)

        cur.execute("""
        INSERT INTO post_snapshots (fetched_at, post_id, like_count, comments_count,
                                    impressions, reach, saved, shares, plays, total_interactions)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fetched_at,
            post_id,
            insights.get("likes", 0),
            insights.get("comments", 0),
            insights.get("impressions"),
            insights.get("reach"),
            insights.get("saved"),
            insights.get("shares"),
            insights.get("plays"),
            insights.get("total_interactions"),
        ))

        if i % 25 == 0:
            log.info(f"  İlerleme: {i}/{len(all_media)} | API istekleri: {fetcher.request_count}")

    conn.commit()
    log.info(f"Tamamlandı! {len(all_media)} post kaydedildi. "
             f"Toplam API isteği: {fetcher.request_count} | Zaman: {fetched_at}")


def main():
    parser = argparse.ArgumentParser(description="Instagram Graph API Data Fetcher")
    parser.add_argument("--loop", action="store_true", help="Saatte bir otomatik çalıştır")
    parser.add_argument("--interval", type=int, default=3600, help="Çalışma aralığı (saniye)")
    args = parser.parse_args()

    # Ortam değişkenlerini kontrol et
    app_id = os.environ.get("IG_APP_ID")
    app_secret = os.environ.get("IG_APP_SECRET")
    access_token = os.environ.get("IG_ACCESS_TOKEN")

    if not all([app_id, app_secret, access_token]):
        print("""
╔══════════════════════════════════════════════════════════════╗
║  .env dosyası oluştur veya ortam değişkenlerini ayarla:     ║
║                                                              ║
║  IG_APP_ID=senin_app_id                                      ║
║  IG_APP_SECRET=senin_app_secret                              ║
║  IG_ACCESS_TOKEN=senin_access_token                          ║
║                                                              ║
║  Opsiyonel:                                                  ║
║  IG_DB_PATH=instagram_data.db                                ║
╚══════════════════════════════════════════════════════════════╝
        """)
        sys.exit(1)

    # Başlat
    token_manager = TokenManager(app_id, app_secret, access_token)
    fetcher = InstagramFetcher(token_manager)
    conn = init_db()

    if args.loop:
        log.info(f"Döngü modu: Her {args.interval} saniyede bir çalışacak.")
        while True:
            try:
                run_fetch_cycle(fetcher, conn)
            except Exception as e:
                log.error(f"Hata: {e}")
            log.info(f"Sonraki çalışma: {args.interval} saniye sonra...")
            time.sleep(args.interval)
    else:
        run_fetch_cycle(fetcher, conn)

    conn.close()


if __name__ == "__main__":
    main()
