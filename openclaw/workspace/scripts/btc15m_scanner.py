#!/usr/bin/env python3
"""
BTC 15M Fast Scanner — runs every 1 minute.
Lightweight: expert analysis → guardian check → order (no debate team).
Designed for speed since BTC 15M windows are only 15 minutes.
Integrates research findings via btc15m_expert.
"""
import json, os, sys, time, fcntl, requests
from datetime import datetime, timezone

SCRIPTS = "/home/ubuntu/.openclaw/workspace/scripts"
sys.path.insert(0, SCRIPTS)

import btc15m_expert

LEDGER_PATH = "/home/ubuntu/.openclaw/workspace/paper_trades.json"
LEDGER = os.path.join(SCRIPTS, "paper_ledger.py")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MAX_OPEN_BTC15M = 3
LOCK_FILE = "/tmp/btc15m_scanner.lock"
LOG_FILE = "/tmp/btc15m_scanner.log"
EVENTS_FILE = "/tmp/trade_events.json"


def send_telegram(msg):
    if BOT_TOKEN and CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
        except:
            pass


def append_event(event_msg):
    """Write trade event to shared file for auto_scan to pick up."""
    import fcntl as _fcntl
    try:
        events = []
        if os.path.exists(EVENTS_FILE):
            with open(EVENTS_FILE) as f:
                _fcntl.flock(f, _fcntl.LOCK_SH)
                try:
                    events = json.load(f)
                except:
                    events = []
        events.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "btc15m",
            "msg": event_msg,
        })
        # Keep only last 50 events
        events = events[-50:]
        with open(EVENTS_FILE, "w") as f:
            _fcntl.flock(f, _fcntl.LOCK_EX)
            json.dump(events, f)
    except:
        pass


def load_ledger():
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except:
        return {"starting_equity": 500, "trades": []}


def build_footer():
    """Build standard footer with Kalshi balances and AI usage."""
    n_open = sum(1 for t in load_ledger()["trades"] if t["status"] == "OPEN")
    total_equity = 0.0
    cash_str = ""
    bal_parts = []
    try:
        from kalshi_trade import api as k_api
        r = k_api("prod", "GET", "/portfolio/balance")
        if r.status_code == 200:
            b = r.json()
            kcash = b.get("balance", 0) / 100
            kpv = b.get("portfolio_value", 0) / 100
            bal_parts.append(f"Kalshi ${kcash + kpv:.2f}")
            cash_str = f"\n\U0001f4b5 Cash available: ${kcash:.2f}"
            total_equity += kcash + kpv
    except:
        pass
    ai_str = ""
    try:
        or_key = os.environ.get("OPENROUTER_API_KEY", "")
        if or_key:
            or_r = requests.get("https://openrouter.ai/api/v1/auth/key",
                                headers={"Authorization": f"Bearer {or_key}"}, timeout=5)
            if or_r.status_code == 200:
                od = or_r.json().get("data", {})
                ai_str = f"\n\U0001f916 AI ${od.get('usage', 0):.2f} used | ${od.get('limit_remaining', 0):.2f} left"
    except:
        pass
    bal_str = " | ".join(bal_parts) if bal_parts else ""
    footer = f"\n\U0001f4c8 {n_open} open | Total ${total_equity:,.2f}"
    if bal_str:
        footer += f"\n\U0001f4b0 {bal_str}"
    footer += cash_str
    footer += ai_str
    # Research status indicator
    research = btc15m_expert.load_research()
    if research.get("edge_decay"):
        footer += "\n\u26a0\ufe0f Research: edge decay — tighter thresholds active"
    elif research.get("timestamp"):
        footer += f"\n\U0001f52c Research: {research['timestamp']}"
    return footer


def open_btc15m_count():
    return sum(1 for t in load_ledger()["trades"]
               if t["status"] == "OPEN" and "KXBTC15M" in t["ticker"])


def total_open_count():
    return sum(1 for t in load_ledger()["trades"] if t["status"] == "OPEN")


def main():
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    full_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    from kalshi_trade import api

    btc15m_expert.update_from_settled(api)

    r = api("prod", "GET", "/markets?series_ticker=KXBTC15M&status=open&limit=5")
    if r.status_code != 200:
        with open(LOG_FILE, "a") as f:
            f.write(f"{full_ts} api_error={r.status_code}\n")
        return

    markets = r.json().get("markets", [])
    if not markets:
        with open(LOG_FILE, "a") as f:
            f.write(f"{full_ts} no_markets\n")
        return

    n_btc15m = open_btc15m_count()
    n_total = total_open_count()

    for m in markets:
        ticker = m["ticker"]
        ya = float(m.get("yes_ask_dollars", "0") or "0")
        yb = float(m.get("yes_bid_dollars", "0") or "0")

        if ya <= 0 and yb <= 0:
            continue
        if n_btc15m >= MAX_OPEN_BTC15M:
            break
        if n_total >= 20:
            break

        open_tickers = {t["ticker"] for t in load_ledger()["trades"] if t["status"] == "OPEN"}
        if ticker in open_tickers:
            continue

        analysis = btc15m_expert.analyze_btc15m_opportunity(m, api)
        if not analysis:
            with open(LOG_FILE, "a") as f:
                mom = btc15m_expert.get_btc_momentum()
                mom_str = f"BTC=${mom['price']:,.0f} {mom['trend']}" if mom else "no_mom"
                f.write(f"{full_ts} {ticker} pass {mom_str} YES={int(ya*100)}c\n")
            continue

        # Cash check
        try:
            bal_r = api("prod", "GET", "/portfolio/balance")
            if bal_r.status_code == 200:
                cash_cents = bal_r.json().get("balance", 0)
                cost_cents = analysis["price"] * analysis["qty"]
                if cost_cents > cash_cents:
                    analysis["qty"] = max(1, cash_cents // max(analysis["price"], 1))
                    if analysis["qty"] < 1 or analysis["price"] * analysis["qty"] > cash_cents:
                        with open(LOG_FILE, "a") as f:
                            f.write(f"{full_ts} {ticker} no_cash: need {cost_cents}c have {cash_cents}c\n")
                        continue
        except:
            pass

        # Guardian check
        try:
            from guardian import veto_trade
            proposal = {
                "ticker": ticker, "price": analysis["price"],
                "qty": analysis["qty"], "market": "kalshi",
                "side": analysis["side"],
            }
            ok, reason = veto_trade(proposal)
            if not ok:
                with open(LOG_FILE, "a") as f:
                    f.write(f"{full_ts} {ticker} guardian_veto: {reason}\n")
                continue
        except:
            pass

        # Place order directly
        try:
            from kalshi_trade import place_order, cancel_order
            fill_price = min(analysis["price"] + 1, 99)
            order = place_order("prod", ticker, "buy", analysis["side"],
                                analysis["qty"], fill_price)
            if not order:
                with open(LOG_FILE, "a") as f:
                    f.write(f"{full_ts} {ticker} order_rejected\n")
                continue

            order_id = order.get("order_id", "")
            time.sleep(5)

            qr = api("prod", "GET", f"/portfolio/orders/{order_id}")
            filled = 0
            if qr.status_code == 200:
                odata = qr.json().get("order", qr.json())
                filled = int(float(odata.get("fill_count_fp", "0")))

            if filled == 0:
                cancel_order("prod", order_id)
                with open(LOG_FILE, "a") as f:
                    f.write(f"{full_ts} {ticker} 0_filled {analysis['side']}@{fill_price}c conv={analysis['conviction']:.0f}\n")
                continue

            actual_qty = filled

            import subprocess
            label = analysis["reason"][:120]
            cmd = (
                f'python3 {LEDGER} open "{ticker}" buy {analysis["price"]} '
                f'{actual_qty} {analysis["stop"]} {analysis["target"]} '
                f'"{label}" kalshi {analysis["side"]} {order_id}'
            )
            subprocess.run(cmd, shell=True, capture_output=True, timeout=30)

            n_btc15m += 1
            n_total += 1

            mom = btc15m_expert.get_btc_momentum()
            summary = btc15m_expert.get_summary()
            footer = build_footer()
            tg_msg = (
                f"\u26a1 BTC 15m Trade — {ts}\n"
                f"{'BUY' if analysis['side'] == 'yes' else 'SELL'} {analysis['side'].upper()} "
                f"{actual_qty}x @ {analysis['price']}c\n"
                f"Conv: {analysis['conviction']:.0f} | "
                f"BTC ${mom['price']:,.0f} {mom['trend']}\n"
                f"{summary}\n"
                f"Stop: {analysis['stop']}c | Target: {analysis['target']}c"
                f"{footer}"
            )
            append_event(tg_msg)

            with open(LOG_FILE, "a") as f:
                f.write(f"{full_ts} {ticker} EXECUTED {analysis['side']} "
                        f"{actual_qty}x@{fill_price}c conv={analysis['conviction']:.0f}\n")

        except Exception as e:
            with open(LOG_FILE, "a") as f:
                f.write(f"{full_ts} {ticker} error: {str(e)[:80]}\n")

    check_btc15m_stops(api)


def check_btc15m_stops(api):
    ledger = load_ledger()
    for t in ledger["trades"]:
        if t["status"] != "OPEN" or "KXBTC15M" not in t["ticker"]:
            continue

        tk = t["ticker"]
        try:
            r = api("prod", "GET", f"/markets/{tk}")
            if r.status_code != 200:
                continue
            md = r.json().get("market", r.json())
            status = md.get("status", "")

            if status == "settled":
                result = md.get("result", "")
                our_side = t.get("broker_side", "yes")
                won = (result == our_side)
                settlement = 100 if won else 0
                action = "TARGET" if won else "STOP"

                import subprocess
                subprocess.run(
                    f'python3 {LEDGER} close {t["id"]} {settlement} "{action}"',
                    shell=True, capture_output=True, timeout=30
                )
                btc15m_expert.record_our_trade(
                    tk, our_side, t.get("entry_price", 0),
                    t.get("qty", 0), "win" if won else "loss"
                )

                outcome = "WIN \u2705" if won else "LOSS"
                pnl = (settlement - t["entry_price"]) * t["qty"] if won else -t["entry_price"] * t["qty"]
                close_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                footer = build_footer()
                settle_msg = (
                    f"\u26a1 BTC 15m {'Win!' if won else 'Loss'} — {close_ts}\n"
                    f"{tk}\n{outcome}: {our_side} settled {result} | PnL: {pnl:+.0f}c\n"
                    f"{btc15m_expert.get_summary()}"
                )
                append_event(settle_msg)

                with open(LOG_FILE, "a") as f:
                    full_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    f.write(f"{full_ts} {tk} SETTLED {result} our={our_side} {'WIN' if won else 'LOSS'}\n")
                continue

            if t.get("broker_side") == "yes":
                cur = int(round(float(md.get("yes_bid_dollars", "0") or "0") * 100))
            else:
                cur = int(round(float(md.get("no_bid_dollars", "0") or "0") * 100))

            if cur <= 0:
                continue

            stop = t.get("stop_loss")
            target = t.get("target")
            if stop and cur <= stop:
                from kalshi_trade import place_order
                place_order("prod", tk, "sell", t.get("broker_side", "yes"), t["qty"], cur)
                import subprocess
                subprocess.run(
                    f'python3 {LEDGER} close {t["id"]} {cur} "STOP"',
                    shell=True, capture_output=True, timeout=30
                )
                btc15m_expert.record_our_trade(tk, t.get("broker_side",""), t["entry_price"], t["qty"], "loss")
            elif target and cur >= target:
                from kalshi_trade import place_order
                place_order("prod", tk, "sell", t.get("broker_side", "yes"), t["qty"], cur)
                import subprocess
                subprocess.run(
                    f'python3 {LEDGER} close {t["id"]} {cur} "TARGET"',
                    shell=True, capture_output=True, timeout=30
                )
                btc15m_expert.record_our_trade(tk, t.get("broker_side",""), t["entry_price"], t["qty"], "win")
        except:
            continue


if __name__ == "__main__":
    main()
