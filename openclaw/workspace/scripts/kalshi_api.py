#!/usr/bin/env python3
"""Kalshi API helper -- supports both key-based and session auth.
Usage:
  python3 scripts/kalshi_api.py events [limit]
  python3 scripts/kalshi_api.py markets <event_ticker> [limit]
  python3 scripts/kalshi_api.py market <market_ticker>
  python3 scripts/kalshi_api.py orderbook <market_ticker>
  python3 scripts/kalshi_api.py candlesticks <market_ticker>
  python3 scripts/kalshi_api.py balance
  python3 scripts/kalshi_api.py positions
"""
import json, os, sys, time, base64, requests

DEMO = os.environ.get("KALSHI_DEMO", "0") == "1"
BASE = "https://demo-api.kalshi.co/trade-api/v2" if DEMO else "https://api.elections.kalshi.com/trade-api/v2"

def create_signature(method, path, timestamp):
    pem_path = os.environ.get("KALSHI_PEM_PATH", "")
    if not pem_path or not os.path.exists(pem_path):
        return None, None
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        with open(pem_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        msg = timestamp + method + path
        sig = key.sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
        return base64.b64encode(sig).decode(), os.environ.get("KALSHI_API_KEY_ID", "")
    except Exception as e:
        print(f"Signature error: {e}", file=sys.stderr)
        return None, None

def api(path, method="GET", auth=False, body=None):
    full_path = "/trade-api/v2" + path
    headers = {"Content-Type": "application/json"}
    if auth:
        ts = str(int(time.time() * 1000))
        sig, key_id = create_signature(method, full_path, ts)
        if sig and key_id:
            headers["KALSHI-ACCESS-KEY"] = key_id
            headers["KALSHI-ACCESS-SIGNATURE"] = sig
            headers["KALSHI-ACCESS-TIMESTAMP"] = ts
    url = BASE.rsplit("/trade-api/v2", 1)[0] + full_path
    if method == "POST":
        r = requests.post(url, headers=headers, json=body)
    else:
        r = requests.get(url, headers=headers)
    return r.json() if r.text else {}

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "events"
    if cmd == "events":
        limit = sys.argv[2] if len(sys.argv) > 2 else "20"
        data = api(f"/events?limit={limit}&status=open")
        for e in data.get("events", []):
            print(f"  {e['event_ticker']:35s} [{e.get('category','')}] {e['title']}")
    elif cmd == "markets":
        ticker = sys.argv[2]
        limit = sys.argv[3] if len(sys.argv) > 3 else "20"
        data = api(f"/markets?event_ticker={ticker}&limit={limit}")
        for m in data.get("markets", []):
            yes_bid = m.get("previous_yes_bid_dollars", "?")
            yes_ask = m.get("previous_yes_ask_dollars", "?")
            vol = m.get("volume", "?")
            print(f"  {m['ticker']:45s} bid={yes_bid} ask={yes_ask} vol={vol} | {m.get('subtitle','')}")
    elif cmd == "market":
        print(json.dumps(api(f"/markets/{sys.argv[2]}"), indent=2))
    elif cmd == "orderbook":
        print(json.dumps(api(f"/markets/{sys.argv[2]}/orderbook"), indent=2))
    elif cmd == "candlesticks":
        print(json.dumps(api(f"/markets/{sys.argv[2]}/candlesticks?period_interval=1"), indent=2))
    elif cmd == "balance":
        data = api("/portfolio/balance", auth=True)
        if "balance" in data:
            print(f"Balance: ${data['balance'] / 100:.2f}")
        else:
            print(json.dumps(data, indent=2))
    elif cmd == "positions":
        print(json.dumps(api("/portfolio/positions", auth=True), indent=2))
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
