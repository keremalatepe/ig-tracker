"""
YouTube OAuth 2.0 - Refresh Token Alma
=======================================
Bir kere çalıştır, refresh token al, GitHub Secret'a kaydet.

Kullanım:
    pip install requests
    python yt_auth.py

İsteyecekleri:
    Client ID     → Google Cloud Console'dan aldığın
    Client Secret → Google Cloud Console'dan aldığın

Çıktı:
    YOUTUBE_REFRESH_TOKEN=xxxxx  ← bunu GitHub Secret'a ekle
"""

import os
import sys
import json
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

REDIRECT_URI = "http://localhost:8080"
SCOPES = " ".join([
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
])

auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>Tamam! Bu sekmeyi kapatabilirsin.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Hata: code parametresi bulunamadi.")

    def log_message(self, *args):
        pass


def get_auth_code(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.timeout = 120

    print("\nTarayıcı açılıyor...")
    print("aybiksbites hesabıyla giriş yap ve izin ver.\n")
    webbrowser.open(url)

    import time
    deadline = time.time() + 120
    while not auth_code and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if not auth_code:
        print("Hata: 120 saniyede yanıt gelmedi.")
        sys.exit(1)

    return auth_code


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    return resp.json()


def main():
    print("=== YouTube Refresh Token Alma ===\n")
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()

    code = get_auth_code(client_id)
    tokens = exchange_code(client_id, client_secret, code)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("\nHata: refresh_token gelmedi. OAuth consent screen'de prompt=consent ayarı gerekli.")
        print("Tam yanıt:", json.dumps(tokens, indent=2))
        sys.exit(1)

    print("\n" + "="*50)
    print("GitHub Secret'a ekle:\n")
    print(f"YOUTUBE_CLIENT_ID={client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={refresh_token}")
    print("="*50)

    with open("yt_tokens.json", "w") as f:
        json.dump({"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token}, f, indent=2)
    print("\nyt_tokens.json dosyasına da kaydedildi (lokal kullanım için).")
    print("Bu dosyayı .gitignore'a ekle, commit'leme!")


if __name__ == "__main__":
    main()
