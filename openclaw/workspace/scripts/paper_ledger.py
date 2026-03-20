#!/usr/bin/env python3
"""Paper trading ledger -- records trades, tracks positions, calculates P&L."""
import json, os, sys, time
from datetime import datetime, timezone

LEDGER_PATH = os.environ.get("PAPER_LEDGER", "/home/ubuntu/.openclaw/workspace/paper_trades.json")

def _load():
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH) as f:
            return json.load(f)
    return {"starting_equity": 10000.0, "trades": [], "positions": {}}

def _save(ledger):
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)

def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def open_trade(ticker, side, price, qty, stop_loss=None, target=None, reason="", market="", broker_side="", order_id=""):
    """Record a new paper trade entry."""
    ledger = _load()
    trade_id = f"T{len(ledger['trades'])+1:04d}"
    mkt = market if market else "kalshi"
    trade = {
        "id": trade_id,
        "ticker": ticker,
        "side": side.upper(),
        "entry_price": float(price),
        "qty": int(qty),
        "stop_loss": float(stop_loss) if stop_loss else None,
        "target": float(target) if target else None,
        "reason": reason,
        "market": mkt,
        "broker_side": broker_side,
        "order_id": order_id,
        "opened_at": _ts(),
        "status": "OPEN",
        "closed_at": None,
        "exit_price": None,
        "pnl": None,
    }
    ledger["trades"].append(trade)
    key = f"{ticker}_{side.upper()}"
    pos = ledger["positions"].get(key, {"qty": 0, "avg_price": 0.0})
    total_qty = pos["qty"] + int(qty)
    if total_qty > 0:
        pos["avg_price"] = (pos["avg_price"] * pos["qty"] + float(price) * int(qty)) / total_qty
    pos["qty"] = total_qty
    ledger["positions"][key] = pos
    _save(ledger)
    print(f"OPENED {trade_id}: {side.upper()} {qty}x {ticker} @ {price} | SL={stop_loss} TGT={target}")
    return trade_id

def close_trade(trade_id, exit_price, reason=""):
    """Close an existing paper trade."""
    ledger = _load()
    for t in ledger["trades"]:
        if t["id"] == trade_id and t["status"] == "OPEN":
            t["exit_price"] = float(exit_price)
            t["closed_at"] = _ts()
            t["status"] = "CLOSED"
            if t["side"] == "BUY":
                raw_pnl = (float(exit_price) - t["entry_price"]) * t["qty"]
            else:
                raw_pnl = (t["entry_price"] - float(exit_price)) * t["qty"]
            is_kalshi = t.get("market") == "kalshi" or t["ticker"].startswith("KX")
            t["pnl"] = round(raw_pnl / 100, 4) if is_kalshi else round(raw_pnl, 4)
            t["close_reason"] = reason
            key = f"{t['ticker']}_{t['side']}"
            if key in ledger["positions"]:
                ledger["positions"][key]["qty"] -= t["qty"]
                if ledger["positions"][key]["qty"] <= 0:
                    del ledger["positions"][key]
            _save(ledger)
            print(f"CLOSED {trade_id}: exit={exit_price} P&L={t['pnl']:+.4f} | {reason}")
            return
    print(f"ERROR: Trade {trade_id} not found or already closed")

def check_stops(current_prices):
    """Check open trades against current prices, close if stop/target hit."""
    ledger = _load()
    closed = []
    for t in ledger["trades"]:
        if t["status"] != "OPEN":
            continue
        ticker = t["ticker"]
        if ticker not in current_prices:
            continue
        price = current_prices[ticker]
        if t["side"] == "BUY":
            if t["stop_loss"] and price <= t["stop_loss"]:
                close_trade(t["id"], price, "STOP_LOSS_HIT")
                closed.append(t["id"])
            elif t["target"] and price >= t["target"]:
                close_trade(t["id"], price, "TARGET_HIT")
                closed.append(t["id"])
        else:
            if t["stop_loss"] and price >= t["stop_loss"]:
                close_trade(t["id"], price, "STOP_LOSS_HIT")
                closed.append(t["id"])
            elif t["target"] and price <= t["target"]:
                close_trade(t["id"], price, "TARGET_HIT")
                closed.append(t["id"])
    return closed

def summary():
    """Print portfolio summary."""
    ledger = _load()
    equity = ledger["starting_equity"]
    total_pnl = 0.0
    wins = 0
    losses = 0
    open_count = 0
    for t in ledger["trades"]:
        if t["status"] == "CLOSED" and t["pnl"] is not None:
            total_pnl += t["pnl"]
            if t["pnl"] > 0:
                wins += 1
            else:
                losses += 1
        elif t["status"] == "OPEN":
            open_count += 1
    current_equity = equity + total_pnl
    total_closed = wins + losses
    win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

    print(f"=== PAPER TRADING SUMMARY ===")
    print(f"Starting equity: ${equity:,.2f}")
    print(f"Current equity:  ${current_equity:,.2f}")
    print(f"Total P&L:       ${total_pnl:+,.4f}")
    print(f"Total trades:    {len(ledger['trades'])} ({open_count} open, {total_closed} closed)")
    print(f"Win rate:        {win_rate:.1f}% ({wins}W / {losses}L)")
    print(f"Max drawdown:    -- (calculated on daily close)")
    print()
    if open_count > 0:
        print("OPEN POSITIONS:")
        for t in ledger["trades"]:
            if t["status"] == "OPEN":
                print(f"  {t['id']}: {t['side']} {t['qty']}x {t['ticker']} @ {t['entry_price']} | SL={t['stop_loss']} TGT={t['target']} | opened {t['opened_at']}")
    if total_closed > 0:
        print("\nRECENT CLOSED:")
        for t in ledger["trades"][-10:]:
            if t["status"] == "CLOSED":
                print(f"  {t['id']}: {t['side']} {t['qty']}x {t['ticker']} @ {t['entry_price']} -> {t['exit_price']} | P&L={t['pnl']:+.4f} | {t.get('close_reason','')}")

def main():
    if len(sys.argv) < 2:
        summary()
        return

    cmd = sys.argv[1]
    if cmd == "open":
        if len(sys.argv) < 5:
            print("Usage: paper_ledger.py open <ticker> <BUY|SELL> <price> <qty> [stop_loss] [target] [reason]")
            sys.exit(1)
        ticker = sys.argv[2]
        side = sys.argv[3]
        price = sys.argv[4]
        qty = sys.argv[5] if len(sys.argv) > 5 else "1"
        sl = sys.argv[6] if len(sys.argv) > 6 else None
        tgt = sys.argv[7] if len(sys.argv) > 7 else None
        reason = sys.argv[8] if len(sys.argv) > 8 else ""
        market = sys.argv[9] if len(sys.argv) > 9 else ""
        broker_side = sys.argv[10] if len(sys.argv) > 10 else ""
        order_id = sys.argv[11] if len(sys.argv) > 11 else ""
        open_trade(ticker, side, price, qty, sl, tgt, reason, market, broker_side, order_id)
    elif cmd == "close":
        if len(sys.argv) < 4:
            print("Usage: paper_ledger.py close <trade_id> <exit_price> [reason]")
            sys.exit(1)
        close_trade(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "manual")
    elif cmd == "summary":
        summary()
    elif cmd == "positions":
        ledger = _load()
        for t in ledger["trades"]:
            if t["status"] == "OPEN":
                print(json.dumps(t))
    elif cmd == "json":
        ledger = _load()
        print(json.dumps(ledger, indent=2))
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: open, close, summary, positions, json")

if __name__ == "__main__":
    main()
