# Instagram Data Tracker

Instagram profesyonel hesabından profil, post, reel, story ve hesap-seviyesi insights verilerini SQLite veritabanına kaydeder. GitHub Actions ile saatte bir arka planda çalışır ve telefondan da açılabilen bir canlı dashboard (Cowork artifact veya tarayıcıdan) üzerinden görselleştirilir.

## Çekilen Veriler

**Profil (her saat):** takipçi, takip, post sayısı, biography, profile picture, website

**Her post/reel (her saat — son 14 gün, 12 saatte bir — eski postlar):**
- Likes, comments, reach, saved, shares, views, total_interactions
- **Follows** — bu posttan gelen takipçi
- **Profile visits, profile activity** — postu görenlerin profile yaptığı eylem
- Reels özel: `ig_reels_avg_watch_time`, `ig_reels_video_view_total_time`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count`

**Story (her saat):** reach, replies, taps_forward, taps_back, exits, views, total_interactions

**Hesap-seviyesi insights (12 saatte bir):** günlük reach, profile_views, accounts_engaged, total_interactions, follower_count delta, website_clicks

**Online followers (12 saatte bir):** saat saat takipçinin online olduğu zaman dilimi (paylaşım zamanlaması için)

**Demografik (haftada bir):** yaş, cinsiyet, ülke, şehir breakdown (Creator hesabında bazıları kısıtlı olabilir)

## Tiered Fetching

API çağrılarını azaltmak ve workflow süresini kısaltmak için modlar:

| Mod      | Ne yapar                                                | Sıklık (auto'da)         |
|----------|---------------------------------------------------------|--------------------------|
| `hourly` | Profil + son 14 gündeki postlar + aktif storyler        | Her saat                 |
| `daily`  | Tüm eski postlar + account insights + online followers  | 6 saatte 1               |
| `weekly` | Audience demographics                                    | 7 günde 1                |
| `full`   | Hepsini bir arada çalıştır                              | Manuel (workflow_dispatch) |
| `auto`   | Cursor'lara bakıp uygun olanları otomatik tetikler      | **Varsayılan**           |

Workflow her saat `auto` modunda çalışır; cursor sistemi sayesinde `daily` ve `weekly` görevler ihtiyaç olduğunda otomatik tetiklenir.

## Kurulum

### 1. Repo

```bash
git init
git add .
git commit -m "İlk kurulum"
git remote add origin https://github.com/KULLANICI/ig-tracker.git
git push -u origin main
```

### 2. GitHub Secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret           | Değer                              |
|------------------|------------------------------------|
| `IG_APP_ID`      | Meta Developer App ID              |
| `IG_APP_SECRET`  | Meta Developer App Secret          |
| `IG_ACCESS_TOKEN`| Developer portalından alınan token |

### 3. Actions İzinleri

Repo → Settings → Actions → General:
- **Read and write permissions** (DB ve dashboard_data.json'ı commit etmesi için)

### 4. İlk Çalıştırma

Actions → "Instagram Data Fetcher" → Run workflow → mode: `full`

İlk full çalışma 2-5 dakika sürebilir (tüm post geçmişi). Sonrası saatlik auto modunda hızlı bitecek.

## Veritabanı Şeması

| Tablo                  | Açıklama                                              |
|------------------------|-------------------------------------------------------|
| `profile_snapshots`    | Her çalışmada profil verisi (takipçi trendi)          |
| `posts`                | Post metadata (caption, tür, link, thumbnail)         |
| `post_snapshots`       | Her çalışmada her postun metrikleri (zaman serisi)    |
| `account_insights`     | Günlük hesap-seviyesi insights                        |
| `online_followers`     | Saat saat takipçi online verisi                       |
| `audience_demographics`| Haftalık demografik dağılım                           |
| `stories`              | Story metadata                                        |
| `story_snapshots`      | Story insights zaman serisi                           |
| `fetch_runs`           | Workflow çalışma log'u (debug için)                   |
| `fetch_cursors`        | Tier'lı fetch için son çalışma zamanları              |

Schema değişikliklerinde `migrate_schema.py` idempotent şekilde uyumlu hale getirir; eski veri korunur.

## Lokal Kullanım

```bash
pip install requests python-dotenv

# .env dosyası
echo "IG_APP_ID=xxx" > .env
echo "IG_APP_SECRET=xxx" >> .env
echo "IG_ACCESS_TOKEN=xxx" >> .env

# Migration (tabloları oluştur/güncelle)
python migrate_schema.py

# Tek seferlik (auto mode)
python ig_fetcher.py

# Belirli mod
python ig_fetcher.py --mode full        # her şey
python ig_fetcher.py --mode daily       # eski postlar + account
python ig_fetcher.py --mode weekly      # demographics

# Sürekli (saatte bir)
python ig_fetcher.py --loop

# JSON export (dashboard için)
python export_for_dashboard.py
```

## Dashboard

`export_for_dashboard.py` her workflow çalışmasında `dashboard_data.json` üretir ve repo'ya commit eder. Mobile-first responsive HTML dashboard bu dosyayı `raw.githubusercontent.com` üzerinden okur. Cowork artifact olarak veya tarayıcıdan açılabilir.

## Workflow Notları

- Cron: her saatin `:17` dakikasında (queue'da daha az yoğun, geç başlama olasılığı düşük)
- GitHub free tier'da peak saatlerde 30-60 dk geç başlayabilir (bu kontrolümüz dışında)
- Concurrency: `ig-fetch` grubu; paralel çalışmaz, sıraya alır
- Timeout: 25 dakika

## API Sınırları

- Meta Graph API: 200 call/saat/user
- `auto` modunda saatlik yük: ~15-30 call (profil + 14 gündeki postlar + storyler)
- `daily` tetiklendiğinde: +50-200 call (post sayısına bağlı)
- `weekly` tetiklendiğinde: +5-15 call

Bu seviyeler 200/saat limitinin altında rahat kalır.
