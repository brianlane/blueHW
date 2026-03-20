#!/usr/bin/env python3
"""
Crypto Scanner v2 — Full trading pipeline for crypto markets.
Follows the same rules as auto_scan.py: scan → debate → RiskGuard → execute → ledger → Telegram.
Trades Kalshi BTC/ETH 15-min + daily markets. Webull discontinued.
Runs every 5 minutes via cron, 24/7.
"""
import json, os, sys, time, subprocess, requests, re, fcntl
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

SCRIPTS = "/home/ubuntu/.openclaw/workspace/scripts"
LEDGER = os.path.join(SCRIPTS, "paper_ledger.py")
sys.path.insert(0, SCRIPTS)
import btc15m_expert

RESEARCH_LOG = "/home/ubuntu/.openclaw/workspace/research_log.json"

MAX_RISK_PER_TRADE = 100       # cents for Kalshi
# Webull discontinued
MAX_CRYPTO_POSITION = 200      # max dollars exposure per crypto trade
MAX_SPREAD_CENTS = 10
MAX_OPEN_POSITIONS = 20
MAX_DEBATE_CANDIDATES = 10
COOLDOWN_FILE = "/tmp/crypto_cooldowns.json"

KALSHI_CRYPTO_SERIES = [
    "KXBTC15M",
    "KXBTCD", "KXETHD", "KXSOLD",
]
# Webull discontinued — all trading is Kalshi-only

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")


def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except:
        pass


def run_cmd(cmd):
    try:
        subprocess.run(cmd, shell=True, capture_output=True, timeout=30)
    except:
        pass


def load_ledger():
    path = "/home/ubuntu/.openclaw/workspace/paper_trades.json"
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {"starting_equity": 10000, "trades": []}


def open_position_count():
    return sum(1 for t in load_ledger()["trades"] if t["status"] == "OPEN")


def get_open_tickers():
    return {t["ticker"] for t in load_ledger()["trades"] if t["status"] == "OPEN"}


# ── Cooldowns ────────────────────────────────────────────────────

def load_cooldowns():
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_cooldowns(cd):
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(cd, f)

def set_cooldown(ticker):
    cd = load_cooldowns()
    cd[ticker] = time.time()
    save_cooldowns(cd)

def is_on_cooldown(ticker):
    cd = load_cooldowns()
    is_15m = "15M" in ticker or "KXBTC15M" in ticker
    cooldown_secs = 900 if is_15m else 3600
    return ticker in cd and time.time() - cd[ticker] < cooldown_secs


# ── Market data ──────────────────────────────────────────────────

def get_crypto_prices():
    """Get BTC, ETH, SOL prices and momentum from Yahoo Finance."""
    results = {}
    symbols = {"BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "SOLUSD": "SOL-USD"}

    def fetch_one(sym, yf_sym):
        try:
            r = requests.get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{yf_sym}?interval=5m&range=4h",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10
            )
            if r.status_code != 200:
                return
            d = r.json()["chart"]["result"][0]
            closes = [c for c in d["indicators"]["quote"][0]["close"] if c is not None]
            volumes = [v for v in d["indicators"]["quote"][0]["volume"] if v is not None]
            if len(closes) < 12:
                return

            cur = closes[-1]
            ma_12 = sum(closes[-12:]) / 12
            ma_24 = sum(closes[-min(24, len(closes)):]) / min(24, len(closes))
            pct_1h = ((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] else 0

            recent = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                      for i in range(-5, 0) if closes[i-1]]
            momentum = sum(recent) / len(recent) if recent else 0

            gains = [c for c in recent if c > 0]
            losses = [-c for c in recent if c < 0]
            avg_g = sum(gains) / len(gains) if gains else 0
            avg_l = sum(losses) / len(losses) if losses else 0.001
            rsi = 100 - (100 / (1 + avg_g / avg_l))

            vol_recent = sum(volumes[-6:]) / 6 if len(volumes) >= 6 else 0
            vol_older = sum(volumes[-12:-6]) / 6 if len(volumes) >= 12 else vol_recent
            vol_ratio = vol_recent / vol_older if vol_older > 0 else 1

            signal = "neutral"
            if cur > ma_12 > ma_24 and momentum > 0.03 and rsi < 70:
                signal = "bullish"
            elif cur < ma_12 < ma_24 and momentum < -0.03 and rsi > 30:
                signal = "bearish"

            results[sym] = {
                "price": cur, "ma_12": ma_12, "ma_24": ma_24,
                "pct_1h": round(pct_1h, 2), "momentum": round(momentum, 3),
                "rsi": round(rsi, 1), "vol_ratio": round(vol_ratio, 2),
                "signal": signal,
            }
        except:
            pass

    with ThreadPoolExecutor(max_workers=3) as pool:
        for sym, yf in symbols.items():
            pool.submit(fetch_one, sym, yf)

    return results


def build_market_summary(prices):
    parts = []
    for sym in ["BTCUSD", "ETHUSD", "SOLUSD"]:
        if sym in prices:
            p = prices[sym]
            name = sym.replace("USD", "")
            parts.append(f"{name}: ${p['price']:,.0f} ({p['pct_1h']:+.1f}%)")
    return " | ".join(parts)


# ── Kalshi crypto scanner ────────────────────────────────────────

def scan_btc15m():
    """Dedicated BTC 15M scanner using the expert module."""
    from kalshi_trade import api
    opps = []

    try:
        r = api("prod", "GET", "/markets?series_ticker=KXBTC15M&status=open&limit=5")
        if r.status_code != 200:
            return opps
        for m in r.json().get("markets", []):
            analysis = btc15m_expert.analyze_btc15m_opportunity(m, api)
            if not analysis:
                continue

            ticker = m["ticker"]
            side = analysis["side"]
            price = analysis["price"]
            subtitle = m.get("subtitle", m.get("yes_sub_title", ""))

            opps.append({
                "ticker": ticker,
                "action": "buy",
                "side": side,
                "price": price,
                "qty": analysis["qty"],
                "stop": analysis["stop"],
                "target": analysis["target"],
                "label": analysis["reason"][:120],
                "market": "kalshi",
                "spread": 1,
                "oi": int(float(m.get("open_interest_fp", "0") or "0")),
                "_btc15m": True,
                "_conviction": analysis["conviction"],
            })
    except Exception as e:
        print(f"BTC 15M scan error: {e}")

    return opps


def scan_kalshi_crypto():
    """Scan all other Kalshi crypto markets (excluding BTC 15M handled by expert)."""
    from kalshi_trade import api
    opps = []

    for series in KALSHI_CRYPTO_SERIES:
        if series == "KXBTC15M":
            continue
        try:
            r = api("prod", "GET", f"/markets?series_ticker={series}&limit=20&status=open")
            if r.status_code != 200:
                continue
            for m in r.json().get("markets", []):
                yb = int(round(float(m.get("yes_bid_dollars", "0") or "0") * 100))
                ya = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
                nb = int(round(float(m.get("no_bid_dollars", "0") or "0") * 100))
                na = int(round(float(m.get("no_ask_dollars", "0") or "0") * 100))
                vol = int(float(m.get("volume_fp", "0") or "0"))
                oi = int(float(m.get("open_interest_fp", "0") or "0"))
                spread = (ya - yb) if ya and yb else 99

                if spread > MAX_SPREAD_CENTS or (vol < 1 and oi < 1):
                    continue

                close_time = m.get("close_time", "")
                title = m.get("title", "")
                subtitle = m.get("subtitle", "")
                ticker = m["ticker"]
                readable = subtitle or title
                hours_left = 0
                try:
                    ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    hours_left = (ct - datetime.now(timezone.utc)).total_seconds() / 3600
                except:
                    pass

                series_name = "BTC" if "BTC" in series else "ETH" if "ETH" in series else "SOL" if "SOL" in series else "Crypto"

                mid = (yb + ya) / 2 if yb and ya else 50
                if mid <= 40 and ya:
                    qty = max(1, min(int(MAX_RISK_PER_TRADE / max(ya, 1)), 50))
                    opps.append({
                        "ticker": ticker, "action": "buy", "side": "yes",
                        "price": ya, "qty": qty, "stop": 1, "target": max(ya * 3, 50),
                        "label": f"BUY YES {series_name}: {readable} @ {ya}c ({hours_left:.0f}h left)",
                        "market": "kalshi", "spread": spread, "oi": oi,
                    })
                elif mid >= 60 and na:
                    cost = 100 - na
                    qty = max(1, min(int(MAX_RISK_PER_TRADE / max(cost, 1)), 50))
                    opps.append({
                        "ticker": ticker, "action": "buy", "side": "no",
                        "price": na, "qty": qty, "stop": 99, "target": max(na, 50),
                        "label": f"BUY NO {series_name}: {readable} @ {na}c ({hours_left:.0f}h left)",
                        "market": "kalshi", "spread": spread, "oi": oi,
                    })
        except:
            continue

    opps.sort(key=lambda x: (x["spread"], -x["oi"]))
    return opps


# ── Webull crypto scanner ────────────────────────────────────────

def scan_webull_crypto(prices):
    return []  # Webull discontinued
    opps = []
    for sym in []:
        if sym not in prices:
            continue
        p = prices[sym]
        name = sym.replace("USD", "")
        price = p["price"]
        signal = p["signal"]
        rsi = p["rsi"]
        momentum = p["momentum"]

        if signal == "bullish" and rsi < 65:
            stop = round(price * 0.97, 2)
            target = round(price * 1.05, 2)
            risk_per_unit = abs(price - stop)
            qty_risk = MAX_CRYPTO_RISK / max(risk_per_unit, 0.01)
            qty_size = MAX_CRYPTO_POSITION / price
            qty = min(qty_risk, qty_size)
            qty = max(round(qty, 8), 0.00000001)
            dollar_val = qty * price
            if dollar_val < 2:
                continue
            opps.append({
                "ticker": sym, "action": "BUY", "side": "BUY",
                "price": round(price, 2), "qty": qty,
                "stop": stop, "target": target,
                "label": f"BUY {name} @ ${price:,.2f} — bullish RSI={rsi} mom={momentum:+.2f}%",
                "market": "webull_crypto",
                "_rr": abs(target - price) / max(abs(price - stop), 0.01),
            })
        elif signal == "bearish" and rsi > 35:
            stop = round(price * 1.03, 2)
            target = round(price * 0.95, 2)
            risk_per_unit = abs(price - stop)
            qty_risk = MAX_CRYPTO_RISK / max(risk_per_unit, 0.01)
            qty_size = MAX_CRYPTO_POSITION / price
            qty = min(qty_risk, qty_size)
            qty = max(round(qty, 8), 0.00000001)
            dollar_val = qty * price
            if dollar_val < 2:
                continue
            opps.append({
                "ticker": sym, "action": "SELL", "side": "SELL",
                "price": round(price, 2), "qty": qty,
                "stop": stop, "target": target,
                "label": f"SELL {name} @ ${price:,.2f} — bearish RSI={rsi} mom={momentum:+.2f}%",
                "market": "webull_crypto",
                "_rr": abs(target - price) / max(abs(price - stop), 0.01),
            })

    opps.sort(key=lambda x: -x.get("_rr", 0))
    return opps


# ── Execution pipeline ───────────────────────────────────────────


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
            "source": "crypto",
            "msg": event_msg,
        })
        events = events[-50:]
        with open(EVENTS_FILE, "w") as f:
            _fcntl.flock(f, _fcntl.LOCK_EX)
            json.dump(events, f)
    except:
        pass


def ptag(market):
    if market == "kalshi":
        return "\U0001f3db"
    elif market != "kalshi":
        return 0.5  # Only Kalshi supported
    return "\U0001f4b9"

def execute_trades(opportunities):
    n_open = open_position_count()
    executed, rejected = [], []

    ticker_counts = {}
    for t in load_ledger()["trades"]:
        if t["status"] == "OPEN":
            tk = t["ticker"]
            ticker_counts[tk] = ticker_counts.get(tk, 0) + t.get("qty", 1)

    candidates = []
    for opp in opportunities:
        if n_open + len(candidates) >= MAX_OPEN_POSITIONS:
            break
        if len(candidates) >= MAX_DEBATE_CANDIDATES:
            break
        if is_on_cooldown(opp["ticker"]):
            continue
        max_contracts = 100 if opp.get("market") == "kalshi" else 10
        if ticker_counts.get(opp["ticker"], 0) >= max_contracts:
            continue
        candidates.append(opp)

    if not candidates:
        return executed, rejected

    try:
        from guardian import check_health
        healthy, msg = check_health()
        if not healthy:
            return executed, [f"\U0001f6d1 Guardian: {msg}"]
    except:
        pass

    # BTC 15M trades with expert conviction bypass debate (expert already analyzed)
    debate_candidates = [c for c in candidates if not c.get("_btc15m")]
    btc15m_candidates = [c for c in candidates if c.get("_btc15m")]

    vote_results = {}
    for btc_opp in btc15m_candidates:
        conv = btc_opp.get("_conviction", 50)
        vote_results[btc_opp["ticker"]] = {
            "approved": True, "yes": 4, "no": 0, "total": 4,
            "summary": f"BTC 15M expert conviction={conv} (debate bypassed)",
        }

    try:
        from debate_team import debate_batch
        if debate_candidates:
            vote_results.update(debate_batch(debate_candidates))
    except:
        pass

    for opp in candidates:
        try:
            from guardian import veto_trade
            ok, reason = veto_trade(opp)
            if not ok:
                rejected.append(f"\U0001f6e1 {ptag(opp['market'])} {opp['label']} \u2014 {reason}")
                continue
        except:
            pass

        vr = vote_results.get(opp["ticker"])
        if not vr:
            rejected.append(f"\u26a0\ufe0f {ptag(opp['market'])} {opp['label']} \u2014 debate failed")
            continue
        if not vr.get("approved"):
            rejected.append(f"\u274c {ptag(opp['market'])} {opp['label']} \u2014 {vr['summary']}")
            continue

        vote_note = f" [{vr['yes']}/{vr['total']}]"
        mkt = opp.get("market", "kalshi")
        order_id = ""

        if mkt == "kalshi":
            try:
                from kalshi_trade import place_order as kalshi_place, api as kalshi_api, cancel_order as kalshi_cancel
                order = kalshi_place("prod", opp["ticker"], opp["action"],
                                     opp.get("side", "yes"), opp["qty"], int(opp["price"]))
                if not order:
                    rejected.append(f"\u274c {ptag(mkt)} {opp['label']} \u2014 Kalshi order rejected")
                    continue
                order_id = order.get("order_id", "")
                time.sleep(2)
                qr = kalshi_api("prod", "GET", f"/portfolio/orders/{order_id}")
                if qr.status_code == 200:
                    odata = qr.json().get("order", qr.json())
                    filled = int(float(odata.get("fill_count_fp", "0")))
                    if filled == 0:
                        kalshi_cancel("prod", order_id)
                        rejected.append(f"\u274c {ptag(mkt)} {opp['label']} \u2014 0 filled, canceled")
                        continue
                    if filled < opp["qty"]:
                        kalshi_cancel("prod", order_id)
                    opp["qty"] = filled
            except Exception as e:
                rejected.append(f"\u274c {ptag(mkt)} {opp['label']} \u2014 Kalshi: {str(e)[:60]}")
                continue

        elif mkt != "kalshi":
            continue  # Only Kalshi supported
        broker_side = opp.get("side", "") if mkt == "kalshi" else ""
        ledger_market = "kalshi"
        qty_str = str(opp["qty"]) if isinstance(opp["qty"], int) else f"{opp['qty']:.8f}"
        cmd = (
            f'python3 {LEDGER} open "{opp["ticker"]}" {opp["action"].upper()} {opp["price"]} '
            f'{qty_str} {opp["stop"]} {opp["target"]} '
            f'"{opp.get("label","")[:120]}" {ledger_market} {broker_side} {order_id}'
        )
        run_cmd(cmd)
        set_cooldown(opp["ticker"])
        executed.append(f"{ptag(mkt)} {qty_str}x {opp['label']}{vote_note}")
        n_open += 1

    return executed, rejected


# ── Stop/target checks ───────────────────────────────────────────

def check_stops(prices):
    closed = []
    ledger = load_ledger()

    for t in ledger["trades"]:
        if t["status"] != "OPEN":
            continue
        tk = t["ticker"]
        mkt = t.get("market", "kalshi")

        cur = None
        if mkt == "kalshi":
            try:
                from kalshi_trade import api
                r = api("prod", "GET", f"/markets/{tk}")
                if r.status_code == 200:
                    md = r.json().get("market", r.json())
                    if t.get("broker_side") == "yes":
                        cur = int(round(float(md.get("yes_bid_dollars", "0") or "0") * 100))
                    else:
                        cur = int(round(float(md.get("no_bid_dollars", "0") or "0") * 100))
            except:
                continue
        elif tk in prices:
            cur = prices[tk]["price"]

        if not cur or cur <= 0:
            continue

        stop = t.get("stop_loss")
        target = t.get("target")
        action = None

        if stop and cur <= stop and t["side"] == "BUY":
            action = "STOP"
        elif target and cur >= target and t["side"] == "BUY":
            action = "TARGET"
        elif stop and cur >= stop and t["side"] == "SELL":
            action = "STOP"
        elif target and cur <= target and t["side"] == "SELL":
            action = "TARGET"

        if action:
            broker_ok = False
            if mkt == "kalshi":
                try:
                    from kalshi_trade import place_order as kalshi_place
                    broker_side = t.get("broker_side", "yes")
                    close_action = "sell" if t["side"] == "BUY" else "buy"
                    result = kalshi_place("prod", tk, close_action, broker_side, t["qty"], int(cur))
                    broker_ok = result is not None
                except:
                    pass
            elif mkt != "kalshi":
                pass  # Only Kalshi supported

            if broker_ok:
                run_cmd(f'python3 {LEDGER} close {t["id"]} {cur} "{action}"')
                set_cooldown(tk)
                label = t.get("reason", tk)[:60]
                price_str = f"${cur/100:.0f}c" if mkt == "kalshi" else f"${cur:,.2f}"
                outcome = "profit ✅" if action == "TARGET" else "loss"
                closed.append(f"{ptag(mkt)} {label} — {action} @ {price_str} ({outcome})")
                if "KXBTC15M" in tk or "15M" in tk:
                    btc15m_expert.record_our_trade(
                        tk, t.get("broker_side", ""), t.get("entry_price", 0),
                        t.get("qty", 0), "win" if action == "TARGET" else "loss"
                    )

    return closed


# ── Main ─────────────────────────────────────────────────────────

def main():
    lock_fp = open("/tmp/crypto_scanner.lock", "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    full_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    prices = get_crypto_prices()
    closed = check_stops(prices)

    btc15m_opps = scan_btc15m()
    kalshi_opps = scan_kalshi_crypto()
    webull_opps = []  # Webull discontinued

    all_opps = btc15m_opps[:]
    ki, si = 0, 0
    all_opps = kalshi_opps[:]

    executed, rejected = execute_trades(all_opps)

    if executed or closed:
        mkt_summary = build_market_summary(prices)
        btc15m_summary = btc15m_expert.get_summary()
        header = f"\U0001f4b0 Crypto Trades \u2014 {ts}"
        if mkt_summary:
            header += f"\n\U0001f30d {mkt_summary}"
        header += f"\n\U0001f4a1 {btc15m_summary}"
        tg_lines = [header]

        if executed:
            tg_lines.append("\n\U0001f7e2 NEW POSITIONS:")
            for e in executed:
                tg_lines.append(f"  \u2022 {e}")
        if closed:
            tg_lines.append("\n\U0001f534 CLOSED:")
            for c in closed:
                tg_lines.append(f"  \u2022 {c}")

        n_open = open_position_count()
        total_equity = 0.0
        bal_parts = []
        kalshi_cash = 0
        try:
            from kalshi_trade import api as kalshi_api
            r = kalshi_api("prod", "GET", "/portfolio/balance")
            if r.status_code == 200:
                bdata = r.json()
                kb = bdata.get("balance", 0) / 100
                kpv = bdata.get("portfolio_value", 0) / 100
                bal_parts.append(f"Kalshi ${kb + kpv:.2f}")
                total_equity += kb + kpv
                kalshi_cash = kb
        except:
            pass
        # Webull discontinued — $500 unused, rate-limited
        pass

        or_str = ""
        try:
            or_key = os.environ.get("OPENROUTER_API_KEY", "")
            if or_key:
                or_r = requests.get("https://openrouter.ai/api/v1/auth/key",
                                     headers={"Authorization": f"Bearer {or_key}"}, timeout=5)
                if or_r.status_code == 200:
                    od = or_r.json().get("data", {})
                    or_str = f"\n\U0001f916 AI ${od.get('usage', 0):.2f} used | ${od.get('limit_remaining', 0):.2f} left"
        except:
            pass

        bal_str = " | ".join(bal_parts) if bal_parts else ""
        footer = f"\n\U0001f4c8 {n_open} open | Total ${total_equity:,.2f}"
        if bal_str:
            footer += f"\n\U0001f4b0 {bal_str}"
        if kalshi_cash > 0:
            footer += f"\n\U0001f4b5 Cash available: ${kalshi_cash:.2f}"
        footer += or_str
        tg_lines.append(footer)
        # Append to shared events file instead of sending direct Telegram
        append_event("\n".join(tg_lines))

    try:
        from kalshi_trade import api as k_api
        btc15m_expert.update_from_settled(k_api)
    except:
        pass
    btc15m_log = f"btc15m={len(btc15m_opps)}"

    if not executed and not closed and btc15m_opps:
        btc15m_summary = btc15m_expert.get_summary()
        mom = btc15m_expert.get_btc_momentum()
        mom_str = f"BTC ${mom['price']:,.0f} | 5m {mom['mom_5m']:+.3f}% | RSI {mom['rsi_1m']}" if mom else "momentum N/A"
        alert_msg = (
            f"\U0001f4a1 BTC 15m Signal \u2014 {ts}\n"
            f"{mom_str}\n"
            f"{btc15m_summary}\n"
            f"Candidates: {len(btc15m_opps)} (pending debate)"
        )

        btc15m_log = f"btc15m={len(btc15m_opps)}"
    with open("/tmp/crypto_scanner.log", "a") as f:
        f.write(f"{full_ts} exec={len(executed)} close={len(closed)} rej={len(rejected)} "
                f"{btc15m_log} kalshi={len(kalshi_opps)}\n")


if __name__ == "__main__":
    main()
