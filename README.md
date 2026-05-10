# Instagram Data Tracker

Instagram profesyonel hesabından saatte bir veri çekip SQLite veritabanına kaydeder.
GitHub Actions ile ücretsiz olarak arka planda çalışır.

## Çekilen Veriler

**Profil:** takipçi sayısı, takip sayısı, post sayısı, bio
**Her post/reel:** likes, comments, impressions, reach, saves, shares, plays

## Kurulum

### 1. Bu repo'yu kendi GitHub hesabına fork'la veya yeni repo oluştur

```bash
git init
git add .
git commit -m "İlk kurulum"
git remote add origin https://github.com/KULLANICI_ADIN/ig-tracker.git
git push -u origin main
```

> ⚠️ Repo'yu **PRIVATE** yap! Veritabanı kişisel verilerini içerecek.

### 2. GitHub Secrets Ekle

Repo → Settings → Secrets and variables → Actions → New repository secret

| Secret Adı | Değer |
|---|---|
| `IG_APP_ID` | Meta Developer App ID |
| `IG_APP_SECRET` | Meta Developer App Secret |
| `IG_ACCESS_TOKEN` | Developer portalından aldığın token |

### 3. Actions İzinlerini Aç

Repo → Settings → Actions → General:
- **"Read and write permissions"** seç (veritabanını commit edebilmesi için)
- **"Allow GitHub Actions to create and approve pull requests"** işaretle

### 4. İlk Çalıştırma

Repo → Actions → "Instagram Data Fetcher" → "Run workflow" butonu ile test et.

Başarılıysa her saat otomatik çalışacak.

## Lokal Kullanım

```bash
pip install requests python-dotenv

# .env dosyası oluştur
echo "IG_APP_ID=xxx" > .env
echo "IG_APP_SECRET=xxx" >> .env
echo "IG_ACCESS_TOKEN=xxx" >> .env

# Tek seferlik
python ig_fetcher.py

# Sürekli (saatte bir)
python ig_fetcher.py --loop
```

## Veritabanı Yapısı

- `profile_snapshots` — Her çalışmada profil verisi (takipçi trendi)
- `posts` — Post bilgileri (caption, tür, link)
- `post_snapshots` — Her çalışmada her postun metrikleri (zaman serisi)
