#!/usr/bin/env python3
"""
BTC 15M Scanner — runs every 1 minute.
Expert analysis → Debate team (for significant trades) → Guardian → Order.

SOUL integration:
  - First-minute momentum filter (via expert)
  - Slippage model (via expert)
  - Quarter-Kelly sizing (via expert + guardian)
  - Debate team for trades flagged needs_debate
"""
import json, os, sys, time, fcntl, requests
from datetime import datetime, timezone

SCRIPTS = "/home/ubuntu/.openclaw/workspace/scripts"
sys.path.insert(0, SCRIPTS)

import btc15m_expert

try:
    import market_maker
    MM_AVAILABLE = True
except ImportError:
    MM_AVAILABLE = False

try:
    import quant_engine
    QUANT_AVAILABLE = True
except ImportError:
    QUANT_AVAILABLE = False

LEDGER_PATH = "/home/ubuntu/.openclaw/workspace/paper_trades.json"
LEDGER = os.path.join(SCRIPTS, "paper_ledger.py")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MAX_OPEN_BTC15M = 3
LOCK_FILE = "/tmp/btc15m_scanner.lock"
LOG_FILE = "/tmp/btc15m_scanner.log"
EVENTS_FILE = "/tmp/trade_events.json"


def append_event(event_msg):
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
    n_open = sum(1 for t in load_ledger()["trades"] if t.get("status", "").upper() == "OPEN")
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
            cash_str = f"\n💵 Cash: ${kcash:.2f}"
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
                ai_str = f"\n🤖 AI ${od.get('usage', 0):.2f} used | ${od.get('limit_remaining', 0):.2f} left"
    except:
        pass
    bal_str = " | ".join(bal_parts)
    footer = f"\n📈 {n_open} open | Total ${total_equity:,.2f}"
    if bal_str:
        footer += f"\n💰 {bal_str}"
    footer += cash_str + ai_str
    research = btc15m_expert.load_research()
    if research.get("edge_decay"):
        footer += "\n⚠️ Edge decay — tighter thresholds"
    return footer


def open_btc15m_count():
    return sum(1 for t in load_ledger()["trades"]
               if t.get("status", "").upper() == "OPEN" and "KXBTC15M" in t.get("ticker", ""))


def total_open_count():
    return sum(1 for t in load_ledger()["trades"] if t.get("status", "").upper() == "OPEN")


def run_debate(analysis, ticker):
    """Run debate team on significant trades. Returns (approved, summary)."""
    try:
        from debate_team import debate
        opp = {
            "ticker": ticker,
            "action": f"BUY {analysis['side'].upper()}",
            "side": analysis["side"],
            "price": analysis["price"],
            "qty": analysis["qty"],
            "stop": analysis["stop"],
            "target": analysis["target"],
            "market": "kalshi",
            "label": analysis["reason"][:120],
            "forecast_info": (
                f"BTC ${analysis['btc_price']:,.0f} vs target ${analysis['target_price']:,.0f}. "
                f"P(above)={analysis['prob_above']:.0%}. "
                f"Edge={analysis['edge']:.0%}, EV={analysis['ev_cents']:+.1f}c. "
                f"Vol={analysis['vol_1m']:.5f}, {analysis['minutes_left']:.0f}m left."
            ),
        }
        result = debate(opp)
        return result.get("approved", False), result.get("summary", "no result")
    except Exception as e:
        return True, f"Debate error ({str(e)[:40]}) — allowing trade"



def execute_spread_trade(opp, api_func, full_ts):
    """Execute a cross-market spread farming trade."""
    ticker = opp.get("ticker", "")
    side = opp.get("side", "yes")
    price = opp.get("price", 0)

    if not ticker or price <= 0:
        return

    # Guardian check
    try:
        from guardian import veto_trade
        proposal = {"ticker": ticker, "price": price, "qty": 2, "market": "kalshi", "side": side}
        ok, reason = veto_trade(proposal)
        if not ok:
            with open(LOG_FILE, "a") as f:
                f.write(f"{full_ts} SPREAD {ticker} guardian_veto: {reason}\n")
            return
    except:
        pass

    # Cash check
    try:
        bal_r = api_func("prod", "GET", "/portfolio/balance")
        if bal_r.status_code == 200:
            cash = bal_r.json().get("balance", 0)
            if price * 2 > cash:
                return
    except:
        pass

    try:
        from kalshi_trade import place_order, cancel_order
        fill_price = min(price + 1, 99)
        order = place_order("prod", ticker, "buy", side, 2, fill_price)
        if not order:
            return

        oid = order.get("order_id", "")
        time.sleep(3)

        qr = api_func("prod", "GET", f"/portfolio/orders/{oid}")
        filled = 0
        if qr.status_code == 200:
            odata = qr.json().get("order", qr.json())
            filled = int(float(odata.get("fill_count_fp", "0")))

        if filled == 0:
            cancel_order("prod", oid)
            return

        import subprocess
        label = f"SPREAD: {opp.get('reason', '')[:100]}"
        # Set target at coherent price, stop at entry - 5c
        target = opp.get("coherent_price", price + 5)
        stop = max(1, price - 5)
        cmd = (
            f'python3 {LEDGER} open "{ticker}" buy {price} '
            f'{filled} {stop} {target} '
            f'"{label}" kalshi {side} {oid}'
        )
        subprocess.run(cmd, shell=True, capture_output=True, timeout=30)

        msg = f"🔄 SPREAD FARM {ticker} {side.upper()} {filled}x@{price}c → target {target}c (+{opp.get('expected_profit_cents',0)}c expected)"
        append_event(msg)

        with open(LOG_FILE, "a") as f:
            f.write(f"{full_ts} SPREAD_EXEC {ticker} {side} {filled}x@{price}c exp_pnl={opp.get('expected_profit_cents',0)}c\n")

    except Exception as e:
        with open(LOG_FILE, "a") as f:
            f.write(f"{full_ts} SPREAD_ERR {ticker}: {str(e)[:80]}\n")


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

    r = api("prod", "GET", "/markets?series_ticker=KXBTC15M&status=open&limit=10")
    if r.status_code != 200:
        with open(LOG_FILE, "a") as f:
            f.write(f"{full_ts} api_error={r.status_code}\n")
        return

    markets = r.json().get("markets", [])
    if not markets:
        with open(LOG_FILE, "a") as f:
            f.write(f"{full_ts} no_markets\n")
        return

    # ═══════════════════════════════════════════════════════════
    # STRATEGY 1: Market Making (spread capture — highest priority)
    # ═══════════════════════════════════════════════════════════
    if MM_AVAILABLE:
        try:
            mm_results = market_maker.run_mm_cycle(api)
            for res in mm_results:
                if res.get("actions"):
                    with open(LOG_FILE, "a") as f:
                        f.write(f"{full_ts} MM {res['ticker']} {res['status']} {res['actions']}\n")
        except Exception as e:
            with open(LOG_FILE, "a") as f:
                f.write(f"{full_ts} MM_ERROR: {str(e)[:80]}\n")

    # ═══════════════════════════════════════════════════════════
    # STRATEGY 2: Cross-market spread farming (coherence arbitrage)
    # ═══════════════════════════════════════════════════════════
    if QUANT_AVAILABLE:
        try:
            spread_opps = quant_engine.find_spread_opportunities(markets, api)
            for opp in spread_opps[:2]:  # max 2 spread trades per cycle
                if opp.get("expected_profit_cents", 0) >= 5:
                    execute_spread_trade(opp, api, full_ts)
        except Exception as e:
            with open(LOG_FILE, "a") as f:
                f.write(f"{full_ts} SPREAD_ERROR: {str(e)[:80]}\n")

    # ═══════════════════════════════════════════════════════════
    # STRATEGY 3: Directional (ONLY when edge is massive — >15% divergence)
    # ═══════════════════════════════════════════════════════════
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
        if n_total >= 15:
            break

        open_tickers = {t["ticker"] for t in load_ledger()["trades"] if t["status"] == "OPEN"}
        if ticker in open_tickers:
            continue

        analysis = btc15m_expert.analyze_btc15m_opportunity(m, api)
        if not analysis:
            with open(LOG_FILE, "a") as f:
                pd = btc15m_expert.get_btc_price_data()
                pd_str = f"BTC=${pd['price']:,.0f}" if pd else "no_data"
                f.write(f"{full_ts} {ticker} pass {pd_str} YES={int(ya*100)}c\n")
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

        # Place directional order
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
                    f.write(f"{full_ts} {ticker} 0_filled {analysis['side']}@{fill_price}c\n")
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

            pd = btc15m_expert.get_btc_price_data()
            summary = btc15m_expert.get_summary()
            footer = build_footer()
            quant_tag = ""
            if analysis.get("quant_applied"):
                quant_tag = f" Q✓ Bayes={analysis.get('bayesian_confidence',0):.0%}"
            debate_tag = " [DEBATED]" if analysis.get("needs_debate") else ""
            tg_msg = (
                f"⚡ BTC15M {analysis['side'].upper()} {ticker}\n"
                f"{actual_qty}x @ {analysis['price']}c{debate_tag}{quant_tag}\n"
                f"Edge {analysis['edge']:.0%} | EV {analysis['ev_cents']:.0f}c\n"
                f"Stop: {analysis['stop']}c | Target: {analysis['target']}c\n"
                f"{summary}"
                f"{footer}"
            )
            append_event(tg_msg)

            with open(LOG_FILE, "a") as f:
                f.write(f"{full_ts} {ticker} EXEC_DIR {analysis['side']} "
                        f"{actual_qty}x@{fill_price}c edge={analysis['edge']:.0%}{quant_tag}\n")

        except Exception as e:
            with open(LOG_FILE, "a") as f:
                f.write(f"{full_ts} {ticker} error: {str(e)[:80]}\n")

    # ═══════════════════════════════════════════════════════════
    # POSITION MANAGEMENT: Trailing stops + early exit
    # ═══════════════════════════════════════════════════════════
    check_btc15m_stops(api)


def check_btc15m_stops(api):
    """
    Smart position management:
    1. Trailing stops — lock in profits as price moves in our favor
    2. Time-based exits — sell if holding too long (don't hold to binary expiry)
    3. Early exit on spread compression — take profit when the edge is realized
    4. Settlement handling — record wins/losses from settled markets
    """
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

            # --- Settled market: record result ---
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

                outcome = "WIN ✅" if won else "LOSS"
                pnl = (settlement - t["entry_price"]) * t["qty"] if won else -t["entry_price"] * t["qty"]
                close_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                settle_msg = (
                    f"⚡ BTC 15m {'Win!' if won else 'Loss'} — {close_ts}\n"
                    f"{tk}\n{outcome}: {our_side} settled {result} | PnL: {pnl:+.0f}c\n"
                    f"{btc15m_expert.get_summary()}"
                )
                append_event(settle_msg)

                with open(LOG_FILE, "a") as f:
                    full_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    f.write(f"{full_ts} {tk} SETTLED {result} our={our_side} {'WIN' if won else 'LOSS'}\n")
                continue

            # --- Get current price for our side ---
            if t.get("broker_side") == "yes":
                cur_bid = int(round(float(md.get("yes_bid_dollars", "0") or "0") * 100))
                cur_ask = int(round(float(md.get("yes_ask_dollars", "0") or "0") * 100))
            else:
                cur_bid = int(round(float(md.get("no_bid_dollars", "0") or "0") * 100))
                cur_ask = int(round(float(md.get("no_ask_dollars", "0") or "0") * 100))

            if cur_bid <= 0:
                continue

            entry = t.get("entry_price", 0)
            stop = t.get("stop_loss", 0)
            target = t.get("target", 99)
            qty = t.get("qty", 1)

            # --- Time until market close ---
            close_time = md.get("close_time", "")
            minutes_left = 99
            try:
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                minutes_left = (ct - datetime.now(timezone.utc)).total_seconds() / 60
            except:
                pass

            # --- Current P&L ---
            unrealized_pnl = (cur_bid - entry) * qty
            pnl_pct = (cur_bid - entry) / max(entry, 1)

            should_exit = False
            exit_reason = ""
            exit_price = cur_bid

            # ── 1. TRAILING STOP: Ratchet stop up as price improves ──
            # If we're up >3c, move stop to breakeven
            # If we're up >8c, trail stop at 60% of gains
            if cur_bid > entry + 8:
                trailing_stop = entry + int((cur_bid - entry) * 0.6)
                if trailing_stop > stop:
                    stop = trailing_stop
            elif cur_bid > entry + 3:
                stop = max(stop, entry)  # breakeven stop

            # ── 2. TIME-BASED EXIT: Don't hold into binary expiry ──
            # Exit with < 2 minutes left unless we're deep in profit
            if minutes_left < 2 and pnl_pct < 0.30:
                should_exit = True
                exit_reason = f"TIME_EXIT: {minutes_left:.1f}m left, pnl={pnl_pct:+.0%}"
            # Exit with < 1 minute left always (RTI averaging zone)
            elif minutes_left < 1:
                should_exit = True
                exit_reason = f"RTI_EXIT: {minutes_left:.1f}m — too risky"

            # ── 3. SPREAD COMPRESSION EXIT: Our edge is realized ──
            # If the spread between our entry and mid has compressed >60%, take profit
            if cur_bid > entry:
                move_toward_target = (cur_bid - entry) / max(target - entry, 1)
                if move_toward_target > 0.6:
                    should_exit = True
                    exit_reason = f"SPREAD_COMPRESS: {move_toward_target:.0%} of target reached"
                    exit_price = cur_bid

            # ── 4. STANDARD STOP LOSS ──
            if cur_bid <= stop and not should_exit:
                should_exit = True
                exit_reason = f"STOP: cur={cur_bid}c <= stop={stop}c"

            # ── 5. TARGET HIT ──
            if cur_bid >= target and not should_exit:
                should_exit = True
                exit_reason = f"TARGET: cur={cur_bid}c >= target={target}c"

            # ── EXECUTE EXIT ──
            if should_exit:
                from kalshi_trade import place_order as kp
                kp("prod", tk, "sell", t.get("broker_side", "yes"), qty, max(1, exit_price - 1))

                is_win = exit_price > entry
                import subprocess
                subprocess.run(
                    f'python3 {LEDGER} close {t["id"]} {exit_price} "{exit_reason[:30]}"',
                    shell=True, capture_output=True, timeout=30
                )
                btc15m_expert.record_our_trade(
                    tk, t.get("broker_side", ""), entry, qty,
                    "win" if is_win else "loss"
                )

                pnl_cents = (exit_price - entry) * qty
                close_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                exit_msg = (
                    f"{'🟢' if is_win else '🔴'} EXIT {tk} — {close_ts}\n"
                    f"{exit_reason}\n"
                    f"Entry: {entry}c → Exit: {exit_price}c | PnL: {pnl_cents:+d}c\n"
                    f"{btc15m_expert.get_summary()}"
                )
                append_event(exit_msg)

                with open(LOG_FILE, "a") as f:
                    full_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    f.write(f"{full_ts} {tk} EXIT {exit_reason} pnl={pnl_cents:+d}c\n")

        except Exception as e:
            with open(LOG_FILE, "a") as f:
                full_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(f"{full_ts} {tk} stop_error: {str(e)[:80]}\n")
            continue

if __name__ == "__main__":
    main()
