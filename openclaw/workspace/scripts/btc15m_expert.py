#!/usr/bin/env python3
"""
BTC 15M Expert — Probability-based intelligence module for KXBTC15M markets.

Uses realized BTC volatility + time remaining to estimate the probability of
BTC being above/below the target price at market expiry. Only trades when
estimated probability gives positive expected value vs the contract price.

Memory file: /tmp/btc15m_memory.json
Research source: /home/ubuntu/.openclaw/workspace/research_log.json
"""
import json, os, time, math, requests
from datetime import datetime, timezone, timedelta

MEMORY_FILE = "/tmp/btc15m_memory.json"
RESEARCH_LOG = "/home/ubuntu/.openclaw/workspace/research_log.json"
MAX_HISTORY = 200

# --- Strategy parameters ---
MIN_EDGE = 0.10          # 10% minimum edge (our_prob - implied_prob) before trading
MIN_MINUTES = 3          # Don't trade with < 3 minutes left
MAX_MINUTES = 14         # Don't trade too early (market hasn't priced correctly yet)
MIN_PRICE = 8            # Don't buy contracts < 8c (lottery tickets)
MAX_PRICE = 92           # Don't buy contracts > 92c (near-certainties, bad R/R)
MIN_VOLUME = 50          # Minimum market volume
MAX_SPREAD = 8           # Maximum bid-ask spread in cents
RISK_PCT = 0.015         # Max 1.5% of cash per trade
MAX_QTY = 10             # Hard cap on contracts per trade
VOLATILITY_LOOKBACK = 25 # Minutes of 1-min candles for volatility


def load_memory():
    try:
        with open(MEMORY_FILE) as f:
            return json.load(f)
    except:
        return {
            "results": [], "our_trades": [],
            "stats": {"total": 0, "yes_wins": 0, "no_wins": 0,
                       "streak": 0, "streak_dir": "none",
                       "our_wins": 0, "our_losses": 0, "our_pnl_cents": 0},
            "last_update": 0,
        }


def save_memory(mem):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2)


def load_research():
    """Load latest research agent findings."""
    try:
        with open(RESEARCH_LOG) as f:
            data = json.load(f)
        reports = data.get("reports", [])
        if not reports:
            return {}
        latest = reports[-1]
        autopsy = latest.get("autopsy", {})
        mc = latest.get("monte_carlo", {})
        decay = latest.get("edge_decay", {})
        return {
            "edge_decay": decay.get("decay_detected", False),
            "recent_win_rate": decay.get("recent_win_rate", 0.5),
            "recent_expectancy": decay.get("recent_expectancy", 0),
            "sharpe": mc.get("sharpe_approx", 0),
            "win_rate": mc.get("win_rate", 0),
            "worst_pattern": max(
                autopsy.get("patterns", {}).items(),
                key=lambda x: x[1], default=("none", 0)
            )[0],
            "lottery_losses": autopsy.get("patterns", {}).get("lottery_ticket_2c", 0),
            "tight_stop_losses": autopsy.get("patterns", {}).get("tight_stop", 0),
            "repeat_losers": list(autopsy.get("repeat_tickers", {}).keys()),
            "timestamp": latest.get("timestamp", ""),
            "historical_summary": data.get("historical_summary", ""),
            "compacted_count": data.get("compacted_count", 0),
        }
    except:
        return {}


def update_from_settled(api_func):
    """Fetch recently settled BTC 15M markets and learn from them."""
    mem = load_memory()
    known_tickers = {r["ticker"] for r in mem["results"]}

    r = api_func("prod", "GET", "/markets?series_ticker=KXBTC15M&status=settled&limit=30")
    if r.status_code != 200:
        return mem

    for m in r.json().get("markets", []):
        tk = m.get("ticker", "")
        if tk in known_tickers:
            continue

        result = m.get("result", "")
        vol = int(float(m.get("volume_fp", "0") or "0"))
        last_price = float(m.get("last_price_dollars", "0") or "0")
        yes_sub = m.get("yes_sub_title", "")

        target_price = 0
        try:
            for part in yes_sub.split("$"):
                if part and part[0].isdigit():
                    target_price = float(part.replace(",", "").split()[0])
                    break
        except:
            pass

        mem["results"].append({
            "ticker": tk, "result": result,
            "target_price": target_price,
            "close_time": m.get("close_time", ""),
            "volume": vol, "last_price": last_price,
            "ts": time.time(),
        })

    mem["results"] = mem["results"][-MAX_HISTORY:]
    mem["results"].sort(key=lambda x: x.get("close_time", ""))

    if mem["results"]:
        stats = mem["stats"]
        stats["total"] = len(mem["results"])
        stats["yes_wins"] = sum(1 for r in mem["results"] if r["result"] == "yes")
        stats["no_wins"] = sum(1 for r in mem["results"] if r["result"] == "no")

        recent = mem["results"][-20:]
        streak = 0
        streak_dir = recent[-1]["result"] if recent else "none"
        for r in reversed(recent):
            if r["result"] == streak_dir:
                streak += 1
            else:
                break
        stats["streak"] = streak
        stats["streak_dir"] = streak_dir
        stats["last_5"] = [r["result"] for r in mem["results"][-5:]]
        stats["last_5_yes_pct"] = sum(1 for r in stats["last_5"] if r == "yes") / max(len(stats["last_5"]), 1)

    mem["last_update"] = time.time()
    save_memory(mem)
    return mem


def normal_cdf(x):
    """Approximate the standard normal CDF (Abramowitz & Stegun)."""
    if x < -6:
        return 0.0
    if x > 6:
        return 1.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x_abs = abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2)
    return 0.5 * (1.0 + sign * y)


def get_btc_price_data():
    """Get BTC price + realized volatility from recent 1-min candles."""
    try:
        r = requests.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1m&range=30m",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8
        )
        if r.status_code != 200:
            return None

        d = r.json()["chart"]["result"][0]
        closes = [c for c in d["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 10:
            return None

        current = closes[-1]

        log_returns = []
        for i in range(1, len(closes)):
            if closes[i] > 0 and closes[i-1] > 0:
                log_returns.append(math.log(closes[i] / closes[i-1]))

        if len(log_returns) < 5:
            return None

        mean_ret = sum(log_returns) / len(log_returns)
        var = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
        vol_1m = math.sqrt(var)

        mom_5m = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 else 0
        mom_15m = (closes[-1] - closes[-16]) / closes[-16] if len(closes) >= 16 else 0

        gains, losses_l = [], []
        for i in range(-min(14, len(closes)-1), 0):
            chg = closes[i] - closes[i-1]
            if chg > 0: gains.append(chg)
            else: losses_l.append(abs(chg))
        avg_g = sum(gains)/len(gains) if gains else 0
        avg_l = sum(losses_l)/len(losses_l) if losses_l else 0.001
        rsi = 100 - (100 / (1 + avg_g / avg_l))

        return {
            "price": current,
            "vol_1m": vol_1m,
            "mom_5m": mom_5m,
            "mom_15m": mom_15m,
            "rsi": rsi,
            "n_candles": len(closes),
        }
    except:
        return None


def estimate_probability(btc_price, target_price, vol_1m, minutes_left, momentum_5m):
    """
    Estimate probability of BTC being above target at expiry.

    Uses volatility-scaled normal distribution with a small momentum drift.
    Returns P(BTC > target) as a float [0, 1].
    """
    if target_price <= 0 or btc_price <= 0 or vol_1m <= 0 or minutes_left <= 0:
        return 0.5

    vol_remaining = vol_1m * math.sqrt(minutes_left)

    drift = momentum_5m * 0.3 * (minutes_left / 5.0)
    drift = max(-0.005, min(0.005, drift))

    move_needed = math.log(target_price / btc_price)

    if vol_remaining < 1e-10:
        return 1.0 if btc_price >= target_price else 0.0

    z = (move_needed - drift) / vol_remaining

    prob_above = 1.0 - normal_cdf(z)
    return max(0.01, min(0.99, prob_above))


def analyze_btc15m_opportunity(market, api_func):
    """
    Analyze a KXBTC15M market using probability-based approach.
    Only returns a trade signal when expected value is positive.
    """
    mem = update_from_settled(api_func)
    price_data = get_btc_price_data()
    if not price_data:
        return None

    research = load_research()

    ticker = market.get("ticker", "")
    ya = int(round(float(market.get("yes_ask_dollars", "0") or "0") * 100))
    yb = int(round(float(market.get("yes_bid_dollars", "0") or "0") * 100))
    na = int(round(float(market.get("no_ask_dollars", "0") or "0") * 100))
    nb = int(round(float(market.get("no_bid_dollars", "0") or "0") * 100))
    vol = int(float(market.get("volume_fp", "0") or "0"))
    close_time = market.get("close_time", "")
    yes_sub = market.get("yes_sub_title", "")

    target_price = 0
    try:
        for part in yes_sub.split("$"):
            if part and part[0].isdigit():
                target_price = float(part.replace(",", "").split()[0])
                break
    except:
        pass

    minutes_left = 0
    try:
        ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        minutes_left = (ct - datetime.now(timezone.utc)).total_seconds() / 60
    except:
        pass

    # --- Hard filters ---
    if minutes_left < MIN_MINUTES or minutes_left > MAX_MINUTES:
        return None
    if vol < MIN_VOLUME:
        return None
    if target_price <= 0:
        return None

    btc_price = price_data["price"]
    vol_1m = price_data["vol_1m"]
    mom_5m = price_data["mom_5m"]

    prob_above = estimate_probability(btc_price, target_price, vol_1m, minutes_left, mom_5m)
    prob_below = 1.0 - prob_above

    implied_yes = ya / 100.0 if ya > 0 else 1.0
    implied_no = na / 100.0 if na > 0 else 1.0

    yes_edge = prob_above - implied_yes
    no_edge = prob_below - implied_no

    # Adjust minimum edge based on research
    effective_min_edge = MIN_EDGE
    if research.get("edge_decay"):
        effective_min_edge = MIN_EDGE + 0.05

    # If our recent win rate is strong, we can be slightly more aggressive
    recent_wr = research.get("recent_win_rate", 0.5)
    if recent_wr > 0.4:
        effective_min_edge = max(0.08, effective_min_edge - 0.02)

    signals = []
    signals.append(f"BTC ${btc_price:,.0f} vs target ${target_price:,.0f} ({(btc_price/target_price - 1)*100:+.3f}%)")
    signals.append(f"P(above)={prob_above:.0%} vol_1m={vol_1m:.5f} {minutes_left:.0f}m left")

    side = None
    price = 0
    edge = 0

    if yes_edge >= effective_min_edge and ya > 0:
        # Check price bounds and spread
        if ya < MIN_PRICE or ya > MAX_PRICE:
            return None
        if ya - yb > MAX_SPREAD:
            return None
        side = "yes"
        price = ya
        edge = yes_edge
        signals.append(f"YES edge: {yes_edge:.1%} (prob={prob_above:.0%} vs ask={ya}c)")

    elif no_edge >= effective_min_edge and na > 0:
        if na < MIN_PRICE or na > MAX_PRICE:
            return None
        if na - nb > MAX_SPREAD:
            return None
        side = "no"
        price = na
        edge = no_edge
        signals.append(f"NO edge: {no_edge:.1%} (prob={prob_below:.0%} vs ask={na}c)")

    else:
        return None

    # --- Kelly-inspired position sizing ---
    # f* = (edge * payoff - (1-edge) * cost) / payoff  simplified for binary
    # Capped at RISK_PCT of cash
    win_payout = 100 - price  # cents profit if we win
    loss_amount = price       # cents lost if we lose
    our_prob = prob_above if side == "yes" else prob_below

    kelly_f = (our_prob * win_payout - (1 - our_prob) * loss_amount) / win_payout
    kelly_f = max(0, min(0.25, kelly_f))

    # Get cash balance for sizing
    cash_cents = 50000  # default $500
    try:
        bal_r = api_func("prod", "GET", "/portfolio/balance")
        if bal_r.status_code == 200:
            cash_cents = bal_r.json().get("balance", 50000)
    except:
        pass

    risk_budget = int(cash_cents * RISK_PCT)
    kelly_budget = int(cash_cents * kelly_f * 0.5)  # half-Kelly for safety
    budget = min(risk_budget, kelly_budget) if kelly_budget > 0 else risk_budget

    qty = max(1, min(budget // max(price, 1), MAX_QTY))

    # Stop and target based on edge and time
    # Tighter stops when time is short, wider when we have time
    time_factor = min(1.0, minutes_left / 10.0)
    stop_pct = 0.25 + 0.15 * time_factor  # 25-40% of entry price
    if research.get("tight_stop_losses", 0) > 15:
        stop_pct += 0.10  # widen stops per research

    if side == "yes":
        stop = max(1, int(price * (1 - stop_pct)))
        target_exit = min(99, price + max(8, int(win_payout * 0.5)))
    else:
        stop = min(99, int(price * (1 + stop_pct)))
        target_exit = max(1, price - max(8, int((100 - price) * 0.5 * (price / 100))))

    ev_cents = our_prob * win_payout - (1 - our_prob) * loss_amount
    reason = (
        f"BTC 15m: {side.upper()} @ {price}c | "
        f"BTC ${btc_price:,.0f} vs ${target_price:,.0f} ({(btc_price/target_price-1)*100:+.3f}%) | "
        f"edge={edge:.0%} EV={ev_cents:+.1f}c/contract | "
        f"{minutes_left:.0f}m left | vol={vol_1m:.5f}"
    )

    return {
        "side": side, "price": price, "qty": qty,
        "stop": stop, "target": target_exit,
        "conviction": edge * 100,
        "reason": reason, "signals": signals,
        "minutes_left": minutes_left,
        "btc_price": btc_price, "target_price": target_price,
        "prob_above": prob_above, "prob_below": prob_below,
        "edge": edge, "ev_cents": ev_cents,
        "vol_1m": vol_1m, "kelly_f": kelly_f,
        "research_applied": bool(research),
    }


def record_our_trade(ticker, side, price, qty, result=None):
    """Record outcome for learning."""
    mem = load_memory()
    entry = {
        "ticker": ticker, "side": side, "price": price,
        "qty": qty, "result": result, "ts": time.time(),
    }
    mem["our_trades"].append(entry)
    mem["our_trades"] = mem["our_trades"][-MAX_HISTORY:]
    if result == "win":
        mem["stats"]["our_wins"] = mem["stats"].get("our_wins", 0) + 1
    elif result == "loss":
        mem["stats"]["our_losses"] = mem["stats"].get("our_losses", 0) + 1
    save_memory(mem)


def get_summary():
    """Get a human-readable summary for Telegram."""
    mem = load_memory()
    stats = mem.get("stats", {})
    total = stats.get("total", 0)
    if total == 0:
        return "BTC 15m: No data yet"

    yes_pct = stats.get("yes_wins", 0) / max(total, 1) * 100
    streak = stats.get("streak", 0)
    streak_dir = stats.get("streak_dir", "?")
    last_5 = stats.get("last_5", [])
    our_w = stats.get("our_wins", 0)
    our_l = stats.get("our_losses", 0)
    our_total = our_w + our_l

    icons = "".join("Y" if r == "yes" else "N" for r in last_5)
    our_str = f" | Ours: {our_w}/{our_total} ({our_w/our_total*100:.0f}%)" if our_total > 0 else ""

    research = load_research()
    tag = ""
    if research.get("edge_decay"):
        tag = " | ⚠ DECAY"
    elif research.get("recent_win_rate", 0) > 0.35:
        tag = " | ✓ OK"

    return (
        f"BTC 15m: {total} tracked | YES {yes_pct:.0f}% "
        f"| streak {streak_dir.upper()}x{streak} | [{icons}]{our_str}{tag}"
    )
