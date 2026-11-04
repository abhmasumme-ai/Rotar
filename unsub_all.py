import os
import sys
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_KEY = os.environ["ROANUZ_PROJECT_KEY"]
API_KEY = os.environ["ROANUZ_API_KEY"]
BASE = "https://api.sports.roanuz.com/v5"

# Docs e confirmed: /unsubscribe/ (Match via Webhook).
# Baki duto docs e dekhini (anumane), tai fail korle ignore korbe.
ENDPOINTS = ["unsubscribe", "detail-updates-unsubscribe", "updates-unsubscribe"]


def get_token():
    r = requests.post(f"{BASE}/core/{PROJECT_KEY}/auth/", json={"api_key": API_KEY}, timeout=20)
    return r.json()["data"]["token"]


def all_match_keys(token):
    r = requests.get(f"{BASE}/cricket/{PROJECT_KEY}/featured-matches-2/",
                     headers={"rs-token": token}, timeout=20)
    matches = (r.json().get("data") or {}).get("matches", [])
    return [m["key"] for m in matches if "key" in m]


def main():
    token = get_token()
    keys = sys.argv[1:] or all_match_keys(token)  # argument dile shudhu oi match gulo
    print(f"Unsubscribing {len(keys)} match(es)...")
    for k in keys:
        for ep in ENDPOINTS:
            try:
                r = requests.post(f"{BASE}/cricket/{PROJECT_KEY}/match/{k}/{ep}/",
                                  headers={"rs-token": token},
                                  json={"method": "web_hook"}, timeout=20)
                print(f"[{r.status_code}] {ep} {k}")
            except Exception as e:
                print(f"[ERR] {ep} {k}: {e}")
            time.sleep(0.2)
    print("Done.")


if __name__ == "__main__":
    main()