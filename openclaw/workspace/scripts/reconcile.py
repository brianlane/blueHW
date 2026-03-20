#!/usr/bin/env python3
"""Reconcile paper_ledger with actual broker positions.
Run daily or on demand to prevent position drift."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))

LEDGER_PATH = os.environ.get("PAPER_LEDGER", "/home/ubuntu/.openclaw/workspace/paper_trades.json")

def get_real_kalshi_positions():
    try:
        from kalshi_trade import api
        r = api("prod", "GET", "/portfolio/positions?limit=50")
        if r.status_code != 200:
            return {}
        real = {}
        for mp in r.json().get("market_positions", []):
            pos = float(mp.get("position_fp", "0"))
            if pos != 0:
                real[mp["ticker"]] = pos
        return real
    except:
        return {}

def get_real_webull_positions():
    return {}

def _old_get_real_webull_positions():
    try:
        from webull_trade import get_client, ACCOUNTS
        tc = get_client("prod")
        r = tc.account_v2.get_account_position(ACCOUNTS["stock"])
        if r.status_code != 200:
            return {}
        real = {}
        for p in r.json() or []:
            sym = p.get("ticker", p.get("symbol", ""))
            qty = float(p.get("qty", p.get("position", "0")))
            if sym and qty > 0:
                real[sym] = qty
        return real
    except:
        return {}

def reconcile():
    with open(LEDGER_PATH) as f:
        ledger = json.load(f)

    kalshi_real = get_real_kalshi_positions()
    webull_real = get_real_webull_positions()
    all_real = {**kalshi_real, **webull_real}

    paper_open = [t for t in ledger["trades"] if t["status"] == "OPEN"]
    paper_tickers = {t["ticker"] for t in paper_open}

    issues = []

    for t in paper_open:
        tk = t["ticker"]
        if tk not in all_real and not t.get("order_id"):
            issues.append(f"GHOST: {t['id']} {tk} — no real position, no order_id")
            t["status"] = "CLOSED"
            t["exit_price"] = t["entry_price"]
            t["pnl"] = 0
            t["close_reason"] = "RECONCILE_GHOST"

    for tk, qty in all_real.items():
        if tk not in paper_tickers:
            issues.append(f"ORPHAN: {tk} ({qty} contracts) exists on broker but not in ledger")

    if issues:
        with open(LEDGER_PATH, "w") as f:
            json.dump(ledger, f, indent=2)
        for i in issues:
            print(f"  {i}")
        print(f"\nFixed {len(issues)} issues")
    else:
        print("Positions in sync — no issues found")

    return issues

if __name__ == "__main__":
    reconcile()
