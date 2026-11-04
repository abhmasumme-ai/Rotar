import os
import sys
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_KEY = os.environ.get("ROANUZ_PROJECT_KEY")
API_KEY = os.environ.get("ROANUZ_API_KEY")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://xxxx.ngrok-free.app/webhook")

INCLUDE_UPCOMING = os.environ.get("INCLUDE_UPCOMING", "0") == "1"
SUBSCRIBE_KIND = os.environ.get("SUBSCRIBE_KIND", "detail").lower()

ENDPOINTS = {
    "detail": ["detail-updates-subscribe"],
    "match": ["subscribe"],
    "all": ["subscribe", "detail-updates-subscribe"],
    "updates": ["updates-subscribe"],
    "both": ["detail-updates-subscribe", "updates-subscribe"],
}.get(SUBSCRIBE_KIND, ["detail-updates-subscribe"])

BASE = "https://api.sports.roanuz.com/v5"
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "60"))
RETRY_SECONDS = 30

subscribed = set()

# HTTP Session setup with automatic retries for DNS / Connection drops
session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=2,  # 2s, 4s, 8s অপেক্ষা করবে
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False
)
session.mount("https://", HTTPAdapter(max_retries=retries))


def say(msg):
    print(msg, flush=True)


def get_token():
    if not PROJECT_KEY or not API_KEY:
        raise ValueError("ROANUZ_PROJECT_KEY or ROANUZ_API_KEY is missing in environment variables!")

    url = f"{BASE}/core/{PROJECT_KEY}/auth/"
    res = session.post(url, json={"api_key": API_KEY}, timeout=15)
    try:
        auth = res.json()
    except ValueError:
        raise RuntimeError(f"Auth response failure (Status {res.status_code}): {res.text}")
    
    if auth.get("data") is None:
        raise RuntimeError(f"Auth failed: {auth}")
    return auth["data"]["token"]


def fetch_live_matches(token):
    url = f"{BASE}/cricket/{PROJECT_KEY}/featured-matches-2/"
    res = session.get(url, headers={"rs-token": token}, timeout=15)
    try:
        data = res.json()
    except ValueError:
        return []

    match_keys = []
    if data.get("data") and "matches" in data["data"]:
        for match in data["data"]["matches"]:
            status = str(match.get("status", "")).lower()
            if "key" not in match:
                continue
            if status == "started":
                match_keys.append(match["key"])
            elif INCLUDE_UPCOMING and status == "not_started":
                match_keys.append(match["key"])
    return match_keys


def subscribe_webhook(token, match_key, endpoint):
    url = f"{BASE}/cricket/{PROJECT_KEY}/match/{match_key}/{endpoint}/"
    res = session.post(
        url,
        headers={"rs-token": token},
        json={"method": "web_hook", "url": WEBHOOK_URL},
        timeout=15
    )

    if res.ok:
        return True

    try:
        err = (res.json().get("error") or {})
    except ValueError:
        err = {}

    # P-400-4 মানে অলরেডি সাবস্ক্রাইবড করা আছে
    if err.get("code") == "P-400-4":
        return True

    say(f"Subscribe failed for {endpoint} - Match {match_key}: Status {res.status_code} | Error: {err}")
    return False


def main():
    say("Starting Roanuz Webhook Auto-Subscriber...")
    token = None

    while True:
        wait = REFRESH_SECONDS
        try:
            # টোকেন না থাকলে বা ফেল করলে নতুন টোকেন জেনারেট করবে
            if not token:
                token = get_token()

            # কাস্টম ম্যাচ কী আর্গুমেন্ট হিসেবে দিলে সেটা নিবে, নয়তো লাইভ ম্যাচ ফেচ করবে
            match_keys = sys.argv[1:]
            if not match_keys:
                match_keys = fetch_live_matches(token)

            new_keys = [k for k in match_keys if any((ep, k) not in subscribed for ep in ENDPOINTS)]

            if not match_keys:
                say("No live matches found. Retrying soon...")
                wait = RETRY_SECONDS
            elif new_keys:
                for k in new_keys:
                    for ep in ENDPOINTS:
                        if (ep, k) in subscribed:
                            continue
                        if subscribe_webhook(token, k, ep):
                            subscribed.add((ep, k))
                            say(f"Successfully subscribed [{ep}] for match: {k}")

        except Exception as e:
            say(f"Execution error: {e}")
            token = None  # এরর হলে পরের বার টোকেন রিফ্রেশ করবে
            wait = RETRY_SECONDS

        time.sleep(wait)


if __name__ == "__main__":
    main()