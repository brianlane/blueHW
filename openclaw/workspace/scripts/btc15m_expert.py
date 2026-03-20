#!/usr/bin/env python3
"""
BTC 15M Expert — Probability-based intelligence for KXBTC15M markets.

Uses:
  - Realized BTC volatility + normal distribution for probability estimation
  - First-minute momentum filter (SOUL mandate)
  - Slippage model (2c flat deducted from edge)
  - Quarter-Kelly (0.25) position sizing (SOUL mandate)
  - Research-driven edge adjustments

Memory: /tmp/btc15m_memory.json
Research: /home/ubuntu/.openclaw/workspace/research_log.json
"""
import json, os, time, math, requests
from datetime import datetime, timezone, timedelta

try:
    import quant_engine
    QUANT_AVAILABLE = True
except ImportError:
    QUANT_AVAILABLE = False

MEMORY_FILE = "/tmp/btc15m_memory.json"
_prev_price_data = {"data": None, "ts": 0}
RESEARCH_LOG = "/home/ubuntu/.openclaw/workspace/research_log.json"
MAX_HISTORY = 200

# --- SOUL-mandated parameters ---
MIN_EDGE = 0.10          # 10% minimum edge after slippage
MIN_EV_GAP = 8           # 8 cents minimum EV per contract (SOUL: >0.08)
SLIPPAGE_CENTS = 2       # Realistic slippage model: 2c per side
MIN_MINUTES = 3          # Don't trade < 3 min remaining
MAX_MINUTES = 13         # Don't trade > 13 min (market still forming)
MIN_PRICE = 10           # No lottery tickets
MAX_PRICE = 90           # No near-certainties
MIN_VOLUME = 80          # Minimum liquidity
MAX_SPREAD = 6           # Maximum bid-ask spread
KELLY_FRACTION = 0.25    # Quarter-Kelly — SOUL mandate
RISK_PCT = 0.01          # Max 1% of cash per trade — SOUL mandate
MAX_QTY = 8              # Hard cap on contracts

# --- First-minute momentum filter thresholds (SOUL: Kalshi-adapted) ---
MOMENTUM_STRONG = 10     # $10+ move = strong confirming factor
MOMENTUM_MEDIUM = 25     # $25+ move = ~68% historical edge
MOMENTUM_HIGH = 50       # $50+ move = 76-99% edge

# --- Volatility floor: don't trade when BTC is too calm for prediction ---
MIN_VOLATILITY = 0.0002  # Floor: if 1-min vol is below this, market is too quiet


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
            "first_minute_cache": {},
        }


def save_memory(mem):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2)


def load_research():
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


def check_market_integrity(ticker, research):
    """
    Check market integrity using research agent's HFT analysis.

    Now distinguishes:
    - legitimate_hft: trust pricing, maybe need higher edge
    - manipulation: avoid or widen edge requirements
    - mixed: proceed with caution
    - exploitable patterns: can give us edge

    Returns (proceed: bool, integrity_adj: float, reason: str)
    """
    if not research:
        return True, 0, "No research data"

    bot_data = research.get("bot_farming", {})
    copy_signals = research.get("copy_signals", [])

    integrity_adj = 0
    reasons = []

    # --- HFT Analysis ---
    if ticker in bot_data:
        market_analysis = bot_data[ticker]
        activity_type = market_analysis.get("type", "unknown")
        severity = market_analysis.get("severity", "low")
        action = market_analysis.get("defense_action", "trust_pricing")
        quality = market_analysis.get("hft_quality_score", 50)
        exploitable = market_analysis.get("exploitable_patterns", [])

        if action == "avoid":
            return False, 0, f"Market integrity: {market_analysis.get('reason', 'manipulation detected')}"

        if action == "trust_pricing":
            # HFT-driven efficient market — need MORE edge to disagree
            integrity_adj -= 0.02
            reasons.append(f"HFT quality={quality}: trust pricing (need more edge)")

        elif action == "widen_edge":
            integrity_adj -= 0.03
            reasons.append(f"Mixed signals: widening edge requirement")

        elif action == "fade_stuffing":
            # Order stuffing detected — we can potentially exploit this
            for ep in exploitable:
                if ep["pattern"] == "fade_stuffing":
                    integrity_adj += 0.02
                    reasons.append(f"Fade stuffing: {ep['detail']}")
                    break

        # Exploit HFT flow direction (smart money signal)
        for ep in exploitable:
            if ep["pattern"] in ("hft_flow_direction", "smart_money_flow"):
                flow_confidence = ep.get("confidence", 0)
                if flow_confidence > 0.4:
                    integrity_adj += min(0.03, flow_confidence * 0.04)
                    reasons.append(f"HFT flow: {ep['detail']} (conf={flow_confidence:.0%})")

        # HFT exit window — temporary opportunity
        for ep in exploitable:
            if ep["pattern"] == "hft_exit_window":
                integrity_adj += 0.02
                reasons.append(f"HFT exit: {ep['detail']}")

    # --- Copy-trading / smart money signals ---
    for sig in copy_signals:
        if sig.get("ticker") == ticker:
            sig_type = sig.get("type", "")
            if "smart_money" in sig_type or "large_order" in sig_type:
                integrity_adj += 0.02
                reasons.append(f"Smart money: {sig.get('detail', '')[:60]}")
            elif "manipulation" in sig_type:
                integrity_adj -= 0.03
                reasons.append(f"Manipulation signal: {sig.get('detail', '')[:60]}")

    reason = " | ".join(reasons) if reasons else "Clean market"
    return True, integrity_adj, reason


def update_from_settled(api_func):
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
            "volume": vol, "ts": time.time(),
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
    if x < -6: return 0.0
    if x > 6: return 1.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x_abs = abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2)
    return 0.5 * (1.0 + sign * y)


def get_btc_price_data():
    """Get BTC price, realized volatility, and momentum from 1-min candles."""
    try:
        r = requests.get(
            "https://query2.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1m&range=30m",
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

        mom_1m = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 else 0
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
            "mom_1m_dollars": mom_1m * current,
            "mom_5m": mom_5m,
            "mom_5m_dollars": mom_5m * current,
            "mom_15m": mom_15m,
            "rsi": rsi,
            "n_candles": len(closes),
        }
    except:
        return None


def estimate_probability(btc_price, target_price, vol_1m, minutes_left, momentum_5m):
    """Estimate P(BTC > target at expiry) using volatility-scaled normal distribution."""
    if target_price <= 0 or btc_price <= 0 or vol_1m <= 0 or minutes_left <= 0:
        return 0.5

    vol_remaining = vol_1m * math.sqrt(minutes_left)
    drift = momentum_5m * 0.2 * (minutes_left / 5.0)
    drift = max(-0.003, min(0.003, drift))
    move_needed = math.log(target_price / btc_price)

    if vol_remaining < 1e-10:
        return 1.0 if btc_price >= target_price else 0.0

    z = (move_needed - drift) / vol_remaining
    prob_above = 1.0 - normal_cdf(z)
    return max(0.01, min(0.99, prob_above))


def first_minute_momentum_check(price_data, target_price, ticker, minutes_left):
    """
    SOUL: First-minute momentum filter.
    After the first ~60s of a market opening, check BTC's move from the target.
    Only amplify signals if momentum confirms direction.
    Returns (pass, confidence_boost, reason).
    """
    if not price_data:
        return False, 0, "No price data"

    btc_price = price_data["price"]
    dollar_move = abs(btc_price - target_price)
    mom_1m = abs(price_data.get("mom_1m_dollars", 0))

    if minutes_left > 13:
        return False, 0, "Market too new — waiting for first-minute data"

    if dollar_move < MOMENTUM_STRONG:
        return False, 0, f"BTC move ${dollar_move:.0f} < ${MOMENTUM_STRONG} minimum"

    boost = 0
    if dollar_move >= MOMENTUM_HIGH:
        boost = 0.10
        reason = f"Strong ${dollar_move:.0f} move from target (76-99% historical edge)"
    elif dollar_move >= MOMENTUM_MEDIUM:
        boost = 0.05
        reason = f"Medium ${dollar_move:.0f} move (~68% edge)"
    else:
        boost = 0.02
        reason = f"Confirming ${dollar_move:.0f} move from target"

    if mom_1m < 5:
        boost *= 0.5
        reason += " (weak 1m momentum — halved)"

    return True, boost, reason


def analyze_btc15m_opportunity(market, api_func):
    """
    Analyze KXBTC15M using probability + first-minute filter + slippage.
    Returns trade dict or None.
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

    if vol_1m < MIN_VOLATILITY:
        return None

    # --- First-minute momentum filter (SOUL mandate) ---
    mom_pass, mom_boost, mom_reason = first_minute_momentum_check(
        price_data, target_price, ticker, minutes_left
    )
    if not mom_pass:
        return None

    # --- Probability estimation ---
    prob_above = estimate_probability(btc_price, target_price, vol_1m, minutes_left, mom_5m)
    prob_below = 1.0 - prob_above

    # --- Slippage model: deduct slippage from effective price ---
    # Buying YES at ya means real cost is ya + SLIPPAGE (worse fill)
    # Selling later also loses slippage. Total round-trip: 2 * SLIPPAGE
    ya_slipped = ya + SLIPPAGE_CENTS if ya > 0 else 0
    na_slipped = na + SLIPPAGE_CENTS if na > 0 else 0

    implied_yes = ya_slipped / 100.0 if ya_slipped > 0 else 1.0
    implied_no = na_slipped / 100.0 if na_slipped > 0 else 1.0

    # --- Market integrity check (SOUL: bot farming + copy trading) ---
    safe, integrity_adj, integrity_reason = check_market_integrity(ticker, research)
    if not safe:
        return None

    # --- Research-driven edge adjustment ---
    effective_min_edge = MIN_EDGE
    if research.get("edge_decay"):
        effective_min_edge = MIN_EDGE + 0.05

    signals = []
    signals.append(f"BTC ${btc_price:,.0f} vs target ${target_price:,.0f} ({(btc_price/target_price-1)*100:+.3f}%)")
    signals.append(f"P(above)={prob_above:.0%} vol={vol_1m:.5f} {minutes_left:.0f}m left")
    signals.append(mom_reason)
    if integrity_adj != 0:
        signals.append(integrity_reason)

    yes_edge = prob_above - implied_yes + (mom_boost if btc_price > target_price else 0) + integrity_adj
    no_edge = prob_below - implied_no + (mom_boost if btc_price < target_price else 0) + integrity_adj

    side = None
    price = 0
    edge = 0

    if yes_edge >= effective_min_edge and ya > 0:
        if ya < MIN_PRICE or ya > MAX_PRICE:
            return None
        if ya - yb > MAX_SPREAD:
            return None
        side = "yes"
        price = ya
        edge = yes_edge
        signals.append(f"YES edge={yes_edge:.1%} (prob={prob_above:.0%}, ask={ya}c, slipped={ya_slipped}c)")

    elif no_edge >= effective_min_edge and na > 0:
        if na < MIN_PRICE or na > MAX_PRICE:
            return None
        if na - nb > MAX_SPREAD:
            return None
        side = "no"
        price = na
        edge = no_edge
        signals.append(f"NO edge={no_edge:.1%} (prob={prob_below:.0%}, ask={na}c, slipped={na_slipped}c)")
    else:
        return None

    # --- Quant engine: Bayesian updates + KL + Bregman + HFT ---
    quant_result = None
    if QUANT_AVAILABLE:
        try:
            # Fetch all open BTC 15M markets for cross-market analysis
            all_markets_r = api_func("prod", "GET", "/markets?series_ticker=KXBTC15M&status=open&limit=10")
            all_open = all_markets_r.json().get("markets", []) if all_markets_r.status_code == 200 else []

            quant_result = quant_engine.full_quant_analysis(
                ticker=ticker,
                our_prob_above=prob_above,
                price_data=price_data,
                target_price=target_price,
                minutes_left=minutes_left,
                all_open_markets=all_open,
                api_func=api_func,
                prev_price_data=_prev_price_data.get("data"),
            )

            # Apply Bayesian posterior
            if quant_result["bayesian_confidence"] > 0.5:
                prob_above = quant_result["adjusted_prob"]
                prob_below = 1.0 - prob_above
                our_prob_for_side = prob_above if side == "yes" else prob_below

            # Apply total edge adjustment from KL + HFT + Bregman
            edge += quant_result["total_edge_adjustment"]

            # If quant stack is highly confident against us, bail
            if quant_result["bayesian_confidence"] > 0.7:
                adj_prob = quant_result["adjusted_prob"]
                if side == "yes" and adj_prob < implied_yes + 0.05:
                    return None
                elif side == "no" and (1 - adj_prob) < implied_no + 0.05:
                    return None

            # Re-check edge after quant adjustments
            if edge < effective_min_edge:
                return None

            signals.append(quant_result["reason"])
        except Exception as e:
            signals.append(f"Quant engine error: {str(e)[:50]}")

    # Cache price data for next Bayesian update
    _prev_price_data["data"] = price_data
    _prev_price_data["ts"] = time.time()

    # --- EV check (SOUL: EV gap > 0.08 = 8c) ---
    win_payout = 100 - price
    loss_amount = price
    our_prob = prob_above if side == "yes" else prob_below
    ev_cents = our_prob * win_payout - (1 - our_prob) * loss_amount - (2 * SLIPPAGE_CENTS)

    if ev_cents < MIN_EV_GAP:
        return None

    # --- Quarter-Kelly position sizing (SOUL mandate) ---
    kelly_f = (our_prob * win_payout - (1 - our_prob) * loss_amount) / win_payout
    kelly_f = max(0, min(0.5, kelly_f))
    quarter_kelly = kelly_f * KELLY_FRACTION

    cash_cents = 40000
    try:
        bal_r = api_func("prod", "GET", "/portfolio/balance")
        if bal_r.status_code == 200:
            cash_cents = bal_r.json().get("balance", 40000)
    except:
        pass

    risk_budget = int(cash_cents * RISK_PCT)
    kelly_budget = int(cash_cents * quarter_kelly)
    budget = min(risk_budget, kelly_budget) if kelly_budget > 0 else risk_budget

    qty = max(1, min(budget // max(price, 1), MAX_QTY))

    # --- Stop/target ---
    time_factor = min(1.0, minutes_left / 10.0)
    stop_pct = 0.30 + 0.10 * time_factor
    if research.get("tight_stop_losses", 0) > 15:
        stop_pct += 0.10

    if side == "yes":
        stop = max(1, int(price * (1 - stop_pct)))
        target_exit = min(99, price + max(8, int(win_payout * 0.5)))
    else:
        stop = min(99, int(price * (1 + stop_pct)))
        target_exit = max(1, price - max(8, int((100 - price) * 0.5 * (price / 100))))

    reason = (
        f"BTC 15m: {side.upper()} @ {price}c | "
        f"BTC ${btc_price:,.0f} vs ${target_price:,.0f} ({(btc_price/target_price-1)*100:+.3f}%) | "
        f"edge={edge:.0%} EV={ev_cents:+.1f}c (after slip) | "
        f"{minutes_left:.0f}m left | {mom_reason}"
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
        "vol_1m": vol_1m, "kelly_f": quarter_kelly,
        "momentum_boost": mom_boost,
        "slippage_applied": SLIPPAGE_CENTS,
        "research_applied": bool(research),
        "needs_debate": ev_cents > 15 or qty >= 4,
        "quant_applied": quant_result is not None,
        "bayesian_confidence": quant_result["bayesian_confidence"] if quant_result else 0,
        "kl_score": quant_result["kl_score"] if quant_result else 0,
        "hft_adjustment": quant_result["hft_adjustment"] if quant_result else 0,
    }


def record_our_trade(ticker, side, price, qty, result=None):
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
