#!/usr/bin/env python3
"""Scan Kalshi for short-term markets + stock prices. Output only actionable data."""
import json, sys, os, time, requests
from datetime import datetime, timezone, timedelta

BASE = "https://api.elections.kalshi.com/trade-api/v2"

SHORT_TERM_SERIES = [
    "KXBTC", "KXETH", "KXXRP15M",
    "KXHIGHNY", "KXHIGHTOKC", "KXHIGHCHI", "KXHIGHLA", "KXHIGHDEN",
    "KXHIGHMIA", "KXHIGHPHX", "KXHIGHSF", "KXHIGHDC",
    "SNOW",
    "INXB", "KXSP500", "KXNASDAQ",
    "KXPROLLS", "KXCPI", "KXNGDPQ", "KXADP",
    "KXBARRLES",
    "KXF1RACE", "KXATPGAME",
    "KXNBA1HSPREAD",
]

WATCHLIST = ["AAPL", "TSLA", "NVDA", "SPY", "QQQ", "AMZN", "GOOGL", "META", "BTC-USD", "ETH-USD"]

def fetch_markets():
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=7)
    results = []
    for series in SHORT_TERM_SERIES:
        try:
            r = requests.get(f"{BASE}/markets?series_ticker={series}&limit=20", timeout=10)
            if r.status_code != 200:
                continue
            for m in r.json().get("markets", []):
                close_str = m.get("close_time", "")
                if not close_str:
                    continue
                try:
                    close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                except:
                    continue
                if close_dt < now or close_dt > cutoff:
                    continue
                yes_bid = float(m.get("previous_yes_bid_dollars", 0) or 0)
                yes_ask = float(m.get("previous_yes_ask_dollars", 0) or 0)
                volume = int(m.get("volume", 0) or 0)
                oi = float(m.get("open_interest_fp", 0) or 0)
                hours_left = (close_dt - now).total_seconds() / 3600
                results.append({
                    "ticker": m["ticker"],
                    "event": m.get("event_ticker", ""),
                    "subtitle": m.get("subtitle", ""),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "spread": round(yes_ask - yes_bid, 4) if yes_ask and yes_bid else None,
                    "volume": volume,
                    "open_interest": oi,
                    "close_time": close_str[:16],
                    "hours_left": round(hours_left, 1),
                })
        except Exception as e:
            pass
    results.sort(key=lambda x: x["hours_left"])
    return results

def fetch_stocks():
    try:
        import yfinance as yf
        data = {}
        for sym in WATCHLIST:
            try:
                t = yf.Ticker(sym)
                p = t.fast_info.last_price
                pc = t.fast_info.previous_close
                chg = ((p - pc) / pc * 100) if pc else 0
                data[sym] = {"price": round(p, 2), "change_pct": round(chg, 2)}
            except:
                pass
        return data
    except ImportError:
        return {}

def main():
    verbose = "--verbose" in sys.argv
    print(f"=== MARKET SCAN {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")
    print()

    markets = fetch_markets()
    stocks = fetch_stocks()

    if markets:
        print(f"KALSHI: {len(markets)} near-term markets (closing within 7 days)")
        for m in markets:
            spread_str = f"spread={m['spread']}" if m['spread'] is not None else "no spread"
            print(f"  {m['ticker']}")
            print(f"    {m['subtitle']} | closes in {m['hours_left']}h | bid={m['yes_bid']} ask={m['yes_ask']} {spread_str} vol={m['volume']} oi={m['open_interest']}")
    else:
        print("KALSHI: No near-term markets found in monitored series.")

    print()
    if stocks:
        print("STOCKS:")
        movers = []
        for sym, d in stocks.items():
            direction = "+" if d["change_pct"] >= 0 else ""
            line = f"  {sym:10s} ${d['price']:>10.2f}  ({direction}{d['change_pct']}%)"
            print(line)
            if abs(d["change_pct"]) >= 1.5:
                movers.append((sym, d["change_pct"]))
        if movers:
            print(f"\n  NOTABLE MOVERS (>1.5%): {', '.join(f'{s} {c:+.1f}%' for s,c in movers)}")

    print()
    print("=== END SCAN ===")

if __name__ == "__main__":
    main()
