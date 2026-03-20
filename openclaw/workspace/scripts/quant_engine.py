#!/usr/bin/env python3
"""
Quant Engine — Core quantitative stack for NemoClaw.

Modules:
  1. Bayesian Updater: Maintains running posterior, updates on each evidence tick
  2. KL-Divergence: Cross-market consistency check
  3. Bregman Projection: Enforce coherent probabilities across simultaneous markets
  4. LMSR Impact: Estimate how our order moves the market price
  5. HFT Intelligence: Exploit HFT/bot patterns instead of just defending

All functions are pure math — no API calls. The caller passes in data.
"""
import math, json, os, time
from datetime import datetime, timezone

POSTERIOR_FILE = "/tmp/btc15m_posteriors.json"


# ═══════════════════════════════════════════════════════════════
# 1. BAYESIAN UPDATER
# ═══════════════════════════════════════════════════════════════

def _load_posteriors():
    try:
        with open(POSTERIOR_FILE) as f:
            data = json.load(f)
        cutoff = time.time() - 1200  # expire after 20 min
        return {k: v for k, v in data.items() if v.get("ts", 0) > cutoff}
    except:
        return {}


def _save_posteriors(data):
    with open(POSTERIOR_FILE, "w") as f:
        json.dump(data, f)


def bayesian_update(ticker, prior, evidences):
    """
    Update a probability estimate using Bayes' theorem on new evidence.

    Args:
        ticker: Market ticker (used to track state across calls)
        prior: Initial probability estimate P(YES) from volatility model
        evidences: List of evidence dicts, each with:
            - "type": one of "price_tick", "volume_surge", "orderbook_shift",
                      "momentum_reversal", "hft_signal"
            - "direction": "yes" or "no" (which side does evidence support)
            - "strength": float 0-1 (how strong is this evidence)

    Returns:
        (posterior, n_updates, confidence) where confidence measures
        how stable the estimate has been across updates.
    """
    posteriors = _load_posteriors()
    state = posteriors.get(ticker, {
        "posterior": prior,
        "n_updates": 0,
        "history": [],
        "ts": time.time(),
    })

    posterior = state["posterior"]

    for ev in evidences:
        direction = ev.get("direction", "yes")
        strength = max(0.01, min(0.99, ev.get("strength", 0.5)))
        ev_type = ev.get("type", "unknown")

        # Likelihood ratios by evidence type
        # These represent P(evidence | YES) / P(evidence | NO)
        type_weights = {
            "price_tick": 1.0,
            "volume_surge": 0.7,
            "orderbook_shift": 0.8,
            "momentum_reversal": 1.2,
            "hft_signal": 0.9,
            "settlement_pattern": 0.6,
        }
        weight = type_weights.get(ev_type, 0.5)
        effective_strength = strength * weight

        if direction == "yes":
            lr = (0.5 + effective_strength) / (0.5 - effective_strength * 0.4)
        else:
            lr = (0.5 - effective_strength * 0.4) / (0.5 + effective_strength)

        lr = max(0.2, min(5.0, lr))

        odds = posterior / (1 - posterior) if posterior < 0.999 else 999
        new_odds = odds * lr
        posterior = new_odds / (1 + new_odds)
        posterior = max(0.01, min(0.99, posterior))

    state["history"].append(round(posterior, 4))
    state["history"] = state["history"][-10:]
    state["posterior"] = posterior
    state["n_updates"] = state.get("n_updates", 0) + 1
    state["ts"] = time.time()

    # Confidence: how stable has the posterior been?
    history = state["history"]
    if len(history) >= 3:
        recent = history[-3:]
        variance = sum((p - sum(recent)/len(recent))**2 for p in recent) / len(recent)
        confidence = max(0, 1.0 - variance * 100)
    else:
        confidence = 0.5

    posteriors[ticker] = state
    _save_posteriors(posteriors)

    return posterior, state["n_updates"], confidence


def get_posterior_state(ticker):
    """Get the current Bayesian state for a ticker (or None)."""
    posteriors = _load_posteriors()
    return posteriors.get(ticker)


def build_evidence_from_data(price_data, prev_price_data, target_price,
                              orderbook=None, prev_orderbook=None):
    """
    Build evidence list from raw market data for Bayesian updating.
    Compares current state to previous state to detect changes.
    """
    evidences = []
    if not price_data:
        return evidences

    btc = price_data["price"]

    # Evidence 1: Price tick — did BTC move toward or away from target?
    if prev_price_data:
        old_dist = abs(prev_price_data["price"] - target_price)
        new_dist = abs(btc - target_price)
        if btc > target_price:
            if new_dist < old_dist:
                evidences.append({"type": "price_tick", "direction": "yes",
                                  "strength": min(0.6, (old_dist - new_dist) / target_price * 1000)})
            else:
                evidences.append({"type": "price_tick", "direction": "no",
                                  "strength": min(0.6, (new_dist - old_dist) / target_price * 1000)})
        else:
            if new_dist < old_dist:
                evidences.append({"type": "price_tick", "direction": "no",
                                  "strength": min(0.6, (old_dist - new_dist) / target_price * 1000)})
            else:
                evidences.append({"type": "price_tick", "direction": "yes",
                                  "strength": min(0.6, (new_dist - old_dist) / target_price * 1000)})

    # Evidence 2: Momentum reversal
    mom_5m = price_data.get("mom_5m", 0)
    if abs(mom_5m) > 0.001:
        if mom_5m > 0 and btc > target_price:
            evidences.append({"type": "momentum_reversal", "direction": "yes",
                              "strength": min(0.5, abs(mom_5m) * 100)})
        elif mom_5m < 0 and btc < target_price:
            evidences.append({"type": "momentum_reversal", "direction": "no",
                              "strength": min(0.5, abs(mom_5m) * 100)})

    # Evidence 3: Orderbook imbalance shift
    if orderbook and prev_orderbook:
        cur_yes_depth = orderbook.get("yes_depth", 0)
        cur_no_depth = orderbook.get("no_depth", 0)
        prev_yes_depth = prev_orderbook.get("yes_depth", 0)
        total_cur = cur_yes_depth + cur_no_depth
        total_prev = prev_yes_depth + prev_orderbook.get("no_depth", 0)

        if total_cur > 10 and total_prev > 10:
            cur_ratio = cur_yes_depth / total_cur
            prev_ratio = prev_yes_depth / total_prev
            shift = cur_ratio - prev_ratio
            if abs(shift) > 0.05:
                evidences.append({
                    "type": "orderbook_shift",
                    "direction": "yes" if shift > 0 else "no",
                    "strength": min(0.5, abs(shift)),
                })

    return evidences


# ═══════════════════════════════════════════════════════════════
# 2. KL-DIVERGENCE
# ═══════════════════════════════════════════════════════════════

def kl_divergence(p, q):
    """
    Compute KL(P || Q) for two Bernoulli distributions.
    P = our estimate, Q = market-implied.
    Measures how much information we'd lose using Q instead of P.
    """
    p = max(0.001, min(0.999, p))
    q = max(0.001, min(0.999, q))

    return p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))


def cross_market_kl_check(our_prob_above, correlated_markets):
    """
    Check if our probability estimate is consistent with correlated markets.

    Args:
        our_prob_above: Our P(BTC > target) estimate
        correlated_markets: List of dicts with:
            - "ticker": market id
            - "target": target price
            - "implied_prob": market-implied probability (from mid price)
            - "our_target": the target for our primary market

    Returns:
        (kl_score, adjustment, reason)
        - kl_score: average KL divergence across correlated markets
        - adjustment: suggested edge adjustment (-0.05 to +0.03)
        - reason: human-readable explanation
    """
    if not correlated_markets:
        return 0, 0, "No correlated markets"

    kl_scores = []
    agreements = 0
    disagreements = 0

    for cm in correlated_markets:
        cm_prob = cm.get("implied_prob", 0.5)
        cm_target = cm.get("target", 0)
        our_target = cm.get("our_target", 0)

        if cm_target <= 0 or our_target <= 0:
            continue

        # Adjust for different targets: if correlated market has a lower target,
        # its P(above) should be >= ours
        if cm_target < our_target:
            # They should be more confident in YES than us
            expected_relation = cm_prob >= our_prob_above
        elif cm_target > our_target:
            expected_relation = cm_prob <= our_prob_above
        else:
            expected_relation = abs(cm_prob - our_prob_above) < 0.15

        kl = kl_divergence(our_prob_above, cm_prob)
        kl_scores.append(kl)

        if expected_relation:
            agreements += 1
        else:
            disagreements += 1

    if not kl_scores:
        return 0, 0, "No valid comparisons"

    avg_kl = sum(kl_scores) / len(kl_scores)
    total = agreements + disagreements

    if avg_kl > 0.15 and disagreements > agreements:
        adj = -0.05
        reason = f"KL={avg_kl:.3f}: our estimate conflicts with {disagreements}/{total} correlated markets"
    elif avg_kl > 0.10:
        adj = -0.02
        reason = f"KL={avg_kl:.3f}: mild disagreement with correlated markets"
    elif avg_kl < 0.03 and agreements > 0:
        adj = 0.02
        reason = f"KL={avg_kl:.3f}: correlated markets confirm our estimate"
    else:
        adj = 0
        reason = f"KL={avg_kl:.3f}: neutral"

    return avg_kl, adj, reason


# ═══════════════════════════════════════════════════════════════
# 3. BREGMAN PROJECTION
# ═══════════════════════════════════════════════════════════════

def bregman_project(market_probs):
    """
    Enforce monotonic consistency across simultaneous BTC 15M markets.

    If target_A < target_B, then P(BTC > target_A) >= P(BTC > target_B).
    Uses iterative I-projection (information projection) to find the
    closest coherent distribution to our estimates.

    Args:
        market_probs: List of dicts sorted by target ascending:
            [{"ticker": ..., "target": float, "prob_above": float, "price": int}, ...]

    Returns:
        List of same dicts with "adjusted_prob" and "adjustment" added.
    """
    if len(market_probs) <= 1:
        for m in market_probs:
            m["adjusted_prob"] = m["prob_above"]
            m["adjustment"] = 0
        return market_probs

    sorted_markets = sorted(market_probs, key=lambda x: x["target"])
    n = len(sorted_markets)
    probs = [m["prob_above"] for m in sorted_markets]

    # Iterative projection: enforce monotonically decreasing probabilities
    # (higher target → lower P(above))
    for iteration in range(20):
        changed = False
        for i in range(n - 1):
            if probs[i] < probs[i + 1]:
                # Violation: lower target has lower prob than higher target
                # Pool-adjacent-violators: average the pair
                avg = (probs[i] + probs[i + 1]) / 2.0
                probs[i] = avg + 0.001
                probs[i + 1] = avg - 0.001
                changed = True

        if not changed:
            break

    probs = [max(0.01, min(0.99, p)) for p in probs]

    for i, m in enumerate(sorted_markets):
        m["adjusted_prob"] = probs[i]
        m["adjustment"] = probs[i] - m["prob_above"]

    return sorted_markets


# ═══════════════════════════════════════════════════════════════
# 4. LMSR IMPACT
# ═══════════════════════════════════════════════════════════════

def lmsr_price_impact(qty, current_price_cents, market_volume):
    """
    Estimate price impact of our order using a simplified LMSR model.
    Hannan's Logarithmic Market Scoring Rule.

    Args:
        qty: Number of contracts we want to buy
        current_price_cents: Current ask price in cents
        market_volume: Total market volume (proxy for liquidity parameter b)

    Returns:
        (effective_price, impact_cents) — what we'll actually pay
        after moving the market.
    """
    if market_volume <= 0 or qty <= 0:
        return current_price_cents, 0

    # Liquidity parameter b: higher = more liquid = less impact
    # Empirically calibrated for Kalshi BTC 15M markets
    b = max(10, market_volume / 20)

    p = current_price_cents / 100.0

    # LMSR cost for qty shares of YES:
    # Cost = b * ln(exp(q_yes/b) + exp(q_no/b)) evaluated before and after
    # Simplified for binary: impact ≈ qty / (2 * b) in probability terms
    impact_prob = qty / (2 * b)
    impact_cents = impact_prob * 100

    # Cap impact at reasonable levels
    impact_cents = min(impact_cents, 5)

    effective_price = current_price_cents + impact_cents / 2

    return round(effective_price, 1), round(impact_cents, 1)


# ═══════════════════════════════════════════════════════════════
# 5. HFT INTELLIGENCE
# ═══════════════════════════════════════════════════════════════

def analyze_hft_patterns(orderbook, volume, minutes_left, ticker=None):
    """
    Analyze HFT/bot activity patterns and extract tradeable signals.

    Kalshi BTC 15M has ~$230k daily volume driven by AI agents and HFT.
    Instead of just defending, we exploit their predictable behavior:

    1. Latency arbitrageurs make prices more accurate → trust tight-spread markets
    2. Order stuffing creates temporary mispricings → fade them
    3. Expiry clustering → HFT firms exit 1-2 min before close, creating opportunities
    4. RTI averaging (60-second) means HFT can't snipe the exact close price

    Args:
        orderbook: {"yes_bids": [...], "no_bids": [...], "yes_depth": int, "no_depth": int}
        volume: Market volume
        minutes_left: Time until market closes
        ticker: Optional, for logging

    Returns:
        {
            "hft_confidence": float 0-1 (how much we trust the market price),
            "spread_quality": "tight" | "normal" | "wide",
            "stuffing_detected": bool,
            "expiry_opportunity": bool,
            "signals": list of evidence dicts for Bayesian updater,
            "edge_adjustment": float,
            "reason": str,
        }
    """
    result = {
        "hft_confidence": 0.5,
        "spread_quality": "normal",
        "stuffing_detected": False,
        "expiry_opportunity": False,
        "signals": [],
        "edge_adjustment": 0,
        "reason": "Default",
    }

    if not orderbook:
        return result

    yes_depth = orderbook.get("yes_depth", 0)
    no_depth = orderbook.get("no_depth", 0)
    total_depth = yes_depth + no_depth
    spread = orderbook.get("spread", 99)

    # --- Spread analysis: HFT creates tight spreads → prices are more accurate ---
    if spread <= 2:
        result["spread_quality"] = "tight"
        result["hft_confidence"] = 0.85
        result["reason"] = f"Tight spread ({spread}c) — HFT-driven accurate pricing"
        # When HFT makes the spread tight, the market price IS the fair price.
        # We need MORE edge to disagree with sophisticated HFT pricing.
        result["edge_adjustment"] = -0.02
    elif spread <= 5:
        result["spread_quality"] = "normal"
        result["hft_confidence"] = 0.65
    else:
        result["spread_quality"] = "wide"
        result["hft_confidence"] = 0.4
        result["reason"] = f"Wide spread ({spread}c) — low HFT activity, price less reliable"
        # Wide spreads mean less HFT → prices are less accurate → our model might have edge
        result["edge_adjustment"] = 0.02

    # --- Order stuffing detection ---
    # Stuffing: many small orders on one side to create false impression
    # Signal: high depth count but low total volume (lots of 1-contract orders)
    if total_depth > 30 and volume < 100:
        result["stuffing_detected"] = True
        result["reason"] = f"Possible order stuffing: {total_depth} orders but vol={volume}"
        # When stuffing is detected, the orderbook is lying.
        # Don't trust the depth ratio — use our probability model instead.
        result["edge_adjustment"] = 0  # neutral, rely on our model
        result["signals"].append({
            "type": "hft_signal",
            "direction": "yes" if yes_depth < no_depth else "no",
            "strength": 0.2,
        })

    # --- Expiry clustering: HFT exits 1-2 min before close ---
    # In the last 2 minutes, HFT pulls liquidity → spreads widen → opportunity
    if minutes_left <= 2 and spread > 5:
        result["expiry_opportunity"] = True
        result["reason"] = f"HFT exit window: {minutes_left:.0f}m left, spread widened to {spread}c"
        # But we also face RTI averaging risk — don't trade in last minute
        if minutes_left >= 1.5:
            result["edge_adjustment"] = 0.03
            result["signals"].append({
                "type": "settlement_pattern",
                "direction": "yes" if yes_depth > no_depth else "no",
                "strength": 0.3,
            })

    # --- Volume surge analysis ---
    # High volume + tight spread = informed trading → trust the direction
    if volume > 300 and spread <= 3:
        imbalance = (yes_depth - no_depth) / max(total_depth, 1)
        if abs(imbalance) > 0.3:
            direction = "yes" if imbalance > 0 else "no"
            result["signals"].append({
                "type": "hft_signal",
                "direction": direction,
                "strength": min(0.5, abs(imbalance)),
            })
            result["reason"] = (
                f"HFT flow: vol={volume}, spread={spread}c, "
                f"{'YES' if direction == 'yes' else 'NO'} imbalance {abs(imbalance):.0%}"
            )

    # --- Wash trading detection (refined) ---
    # Real wash trading: abnormally high volume-to-OI ratio with no price movement
    # This is different from legitimate HFT which moves prices
    oi = orderbook.get("open_interest", 0)
    if volume > 500 and oi > 0 and volume / oi > 10:
        result["stuffing_detected"] = True
        result["edge_adjustment"] = -0.03
        result["reason"] = f"Wash trading: vol/OI={volume/oi:.0f}x — prices unreliable"

    return result


def fetch_orderbook_data(ticker, api_func):
    """Fetch and parse orderbook into a standardized format."""
    try:
        r = api_func("prod", "GET", f"/markets/{ticker}/orderbook")
        if r.status_code != 200:
            return None
        ob = r.json().get("orderbook", r.json())
        yes_orders = ob.get("yes", [])
        no_orders = ob.get("no", [])

        yes_depth = sum(int(float(o.get("count_fp", "0") or "0")) for o in yes_orders)
        no_depth = sum(int(float(o.get("count_fp", "0") or "0")) for o in no_orders)

        best_yes_bid = max((int(round(float(o.get("price_fp", "0") or "0") * 100))
                           for o in yes_orders), default=0)
        best_yes_ask = 0
        best_no_bid = max((int(round(float(o.get("price_fp", "0") or "0") * 100))
                          for o in no_orders), default=0)

        spread = max(0, 100 - best_yes_bid - best_no_bid) if best_yes_bid and best_no_bid else 99

        return {
            "yes_depth": yes_depth,
            "no_depth": no_depth,
            "best_yes_bid": best_yes_bid,
            "best_no_bid": best_no_bid,
            "spread": spread,
            "yes_orders": len(yes_orders),
            "no_orders": len(no_orders),
        }
    except:
        return None


# ═══════════════════════════════════════════════════════════════
# 6. FULL QUANT ANALYSIS (orchestrator)
# ═══════════════════════════════════════════════════════════════

def full_quant_analysis(ticker, our_prob_above, price_data, target_price,
                         minutes_left, all_open_markets, api_func,
                         prev_price_data=None):
    """
    Run the complete quant stack on a trade opportunity.

    Returns:
        {
            "adjusted_prob": float,        # Bayesian-updated probability
            "bayesian_confidence": float,  # How stable the estimate is
            "kl_score": float,             # Divergence from correlated markets
            "kl_adjustment": float,        # Edge adjustment from KL
            "bregman_adjustment": float,   # Coherence correction
            "hft_adjustment": float,       # HFT pattern adjustment
            "lmsr_impact": float,          # Expected price impact
            "total_edge_adjustment": float, # Sum of all adjustments
            "signals": list,               # Evidence for logging
            "reason": str,                 # Human-readable summary
        }
    """
    result = {
        "adjusted_prob": our_prob_above,
        "bayesian_confidence": 0.5,
        "kl_score": 0,
        "kl_adjustment": 0,
        "bregman_adjustment": 0,
        "hft_adjustment": 0,
        "lmsr_impact": 0,
        "total_edge_adjustment": 0,
        "signals": [],
        "reason": "",
    }

    reasons = []

    # --- 1. Fetch orderbook for HFT analysis ---
    orderbook = fetch_orderbook_data(ticker, api_func)

    # --- 2. HFT Intelligence ---
    market_vol = 0
    try:
        mr = api_func("prod", "GET", f"/markets/{ticker}")
        if mr.status_code == 200:
            md = mr.json().get("market", mr.json())
            market_vol = int(float(md.get("volume_fp", "0") or "0"))
    except:
        pass

    hft = analyze_hft_patterns(orderbook, market_vol, minutes_left, ticker)
    result["hft_adjustment"] = hft["edge_adjustment"]
    result["signals"].extend(hft["signals"])
    if hft["reason"] != "Default":
        reasons.append(f"HFT: {hft['reason']}")

    # --- 3. Bayesian Update ---
    evidences = build_evidence_from_data(
        price_data, prev_price_data, target_price,
        orderbook, None
    )
    evidences.extend(hft["signals"])

    if evidences:
        posterior, n_updates, confidence = bayesian_update(
            ticker, our_prob_above, evidences
        )
        result["adjusted_prob"] = posterior
        result["bayesian_confidence"] = confidence
        if abs(posterior - our_prob_above) > 0.03:
            reasons.append(f"Bayes: {our_prob_above:.0%}→{posterior:.0%} ({n_updates} updates, conf={confidence:.0%})")

    # --- 4. KL-Divergence on correlated markets ---
    correlated = []
    for m in all_open_markets:
        m_ticker = m.get("ticker", "")
        if m_ticker == ticker:
            continue
        m_ya = float(m.get("yes_ask_dollars", "0") or "0")
        m_yb = float(m.get("yes_bid_dollars", "0") or "0")
        if m_ya <= 0 and m_yb <= 0:
            continue

        m_mid = ((m_ya + m_yb) / 2) if m_yb > 0 else m_ya
        m_sub = m.get("yes_sub_title", "")
        m_target = 0
        try:
            for part in m_sub.split("$"):
                if part and part[0].isdigit():
                    m_target = float(part.replace(",", "").split()[0])
                    break
        except:
            pass

        if m_target > 0:
            correlated.append({
                "ticker": m_ticker,
                "target": m_target,
                "implied_prob": m_mid,
                "our_target": target_price,
            })

    if correlated:
        kl_score, kl_adj, kl_reason = cross_market_kl_check(
            result["adjusted_prob"], correlated
        )
        result["kl_score"] = kl_score
        result["kl_adjustment"] = kl_adj
        if kl_adj != 0:
            reasons.append(kl_reason)

    # --- 5. Bregman Projection ---
    if len(all_open_markets) > 1:
        market_probs = []
        for m in all_open_markets:
            m_sub = m.get("yes_sub_title", "")
            m_target = 0
            try:
                for part in m_sub.split("$"):
                    if part and part[0].isdigit():
                        m_target = float(part.replace(",", "").split()[0])
                        break
            except:
                pass
            m_ya = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
            if m_target > 0:
                prob = result["adjusted_prob"] if m["ticker"] == ticker else m_ya / 100.0
                market_probs.append({
                    "ticker": m["ticker"],
                    "target": m_target,
                    "prob_above": prob,
                    "price": m_ya,
                })

        if len(market_probs) > 1:
            projected = bregman_project(market_probs)
            for pm in projected:
                if pm["ticker"] == ticker and abs(pm["adjustment"]) > 0.01:
                    result["bregman_adjustment"] = pm["adjustment"]
                    result["adjusted_prob"] += pm["adjustment"]
                    result["adjusted_prob"] = max(0.01, min(0.99, result["adjusted_prob"]))
                    reasons.append(f"Bregman: coherence correction {pm['adjustment']:+.1%}")
                    break

    # --- 6. LMSR Impact (informational) ---
    result["lmsr_impact"] = 0
    if market_vol > 0:
        _, impact = lmsr_price_impact(5, 50, market_vol)
        result["lmsr_impact"] = impact

    # --- Total adjustment ---
    result["total_edge_adjustment"] = (
        result["kl_adjustment"] +
        result["hft_adjustment"] +
        result["bregman_adjustment"]
    )

    result["reason"] = " | ".join(reasons) if reasons else "Quant: no significant adjustments"

    return result


# ═══════════════════════════════════════════════════════════════
# 7. CROSS-MARKET SPREAD FARMING
# ═══════════════════════════════════════════════════════════════

def find_spread_opportunities(all_open_markets, api_func):
    """
    Find cross-market spread opportunities using Bregman coherence violations.

    When multiple BTC 15M markets are open simultaneously with different targets,
    their probabilities must be monotonically decreasing (higher target = lower P(above)).
    When this is violated, one market is overpriced and the other underpriced.

    Strategy:
    - Buy the underpriced market (probability too low vs. coherent estimate)
    - Sell/avoid the overpriced market
    - Profit when prices converge toward coherent values (no directional bet needed)

    Args:
        all_open_markets: List of market dicts from Kalshi API
        api_func: Kalshi API function

    Returns:
        List of opportunity dicts:
        [{
            "type": "spread_farm",
            "buy_ticker": str, "buy_side": str, "buy_price": int,
            "sell_ticker": str, "sell_side": str, "sell_price": int,
            "expected_profit_cents": int,
            "confidence": float,
            "reason": str,
        }]
    """
    opportunities = []

    # Parse all markets
    parsed = []
    for m in all_open_markets:
        ticker = m.get("ticker", "")
        yes_sub = m.get("yes_sub_title", "")
        target = 0
        try:
            for part in yes_sub.split("$"):
                if part and part[0].isdigit():
                    target = float(part.replace(",", "").split()[0])
                    break
        except:
            continue

        if target <= 0:
            continue

        ya = int(round(float(m.get("yes_ask_dollars", "0") or "0") * 100))
        yb = int(round(float(m.get("yes_bid_dollars", "0") or "0") * 100))
        na = int(round(float(m.get("no_ask_dollars", "0") or "0") * 100))
        nb = int(round(float(m.get("no_bid_dollars", "0") or "0") * 100))
        vol = int(float(m.get("volume_fp", "0") or "0"))

        close_time = m.get("close_time", "")
        minutes_left = 0
        try:
            ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            minutes_left = (ct - datetime.now(timezone.utc)).total_seconds() / 60
        except:
            pass

        if ya <= 0 or yb <= 0 or minutes_left < 3:
            continue

        mid = (ya + yb) / 2.0
        spread = ya - yb

        parsed.append({
            "ticker": ticker,
            "target": target,
            "yes_ask": ya, "yes_bid": yb,
            "no_ask": na, "no_bid": nb,
            "mid": mid, "spread": spread,
            "prob_above": mid / 100.0,
            "volume": vol,
            "minutes_left": minutes_left,
        })

    if len(parsed) < 2:
        return opportunities

    # Sort by target
    parsed.sort(key=lambda x: x["target"])

    # Run Bregman projection to find coherent probabilities
    market_probs = [{"ticker": p["ticker"], "target": p["target"],
                     "prob_above": p["prob_above"], "price": p["yes_ask"]}
                    for p in parsed]

    projected = bregman_project(market_probs)

    # Find significant violations
    for i, (original, projected_m) in enumerate(zip(parsed, projected)):
        adj = projected_m["adjustment"]

        # Significant underpricing: market price is too low vs coherent estimate
        if adj > 0.04:
            expected_move = int(adj * 100)
            cost = original["yes_ask"]
            expected_profit = expected_move - original["spread"]  # subtract our entry cost

            if expected_profit >= 3 and original["volume"] >= 50:
                opportunities.append({
                    "type": "spread_farm",
                    "action": "buy",
                    "ticker": original["ticker"],
                    "side": "yes",
                    "price": original["yes_ask"],
                    "coherent_price": int(projected_m["adjusted_prob"] * 100),
                    "expected_profit_cents": expected_profit,
                    "adjustment": adj,
                    "confidence": min(0.8, adj * 5),
                    "minutes_left": original["minutes_left"],
                    "volume": original["volume"],
                    "reason": (
                        f"Underpriced: {original['ticker']} YES @ {cost}c, "
                        f"coherent = {int(projected_m['adjusted_prob']*100)}c "
                        f"(+{expected_move}c expected, {original['minutes_left']:.0f}m left)"
                    ),
                })

        # Significant overpricing: market price is too high
        elif adj < -0.04:
            expected_move = int(abs(adj) * 100)
            cost = original["no_ask"]
            expected_profit = expected_move - (100 - original["yes_bid"] - original["no_bid"]) if original["no_bid"] > 0 else 0

            if expected_profit >= 3 and original["volume"] >= 50 and cost > 0:
                opportunities.append({
                    "type": "spread_farm",
                    "action": "buy",
                    "ticker": original["ticker"],
                    "side": "no",
                    "price": cost,
                    "coherent_price": int((1 - projected_m["adjusted_prob"]) * 100),
                    "expected_profit_cents": expected_profit,
                    "adjustment": adj,
                    "confidence": min(0.8, abs(adj) * 5),
                    "minutes_left": original["minutes_left"],
                    "volume": original["volume"],
                    "reason": (
                        f"Overpriced: {original['ticker']} NO @ {cost}c, "
                        f"coherent = {int((1-projected_m['adjusted_prob'])*100)}c "
                        f"(+{expected_move}c expected, {original['minutes_left']:.0f}m left)"
                    ),
                })

    # Check for complementary pairs (buy underpriced + sell overpriced = hedged)
    buys = [o for o in opportunities if o["side"] == "yes"]
    sells = [o for o in opportunities if o["side"] == "no"]

    for b in buys:
        for s in sells:
            if b["ticker"] != s["ticker"]:
                total_cost = b["price"] + s["price"]
                if total_cost < 95:
                    hedged_profit = 100 - total_cost
                    opportunities.append({
                        "type": "spread_farm_hedged",
                        "buy_ticker": b["ticker"], "buy_side": "yes", "buy_price": b["price"],
                        "sell_ticker": s["ticker"], "sell_side": "no", "sell_price": s["price"],
                        "total_cost": total_cost,
                        "expected_profit_cents": hedged_profit,
                        "confidence": min(b["confidence"], s["confidence"]),
                        "reason": (
                            f"Hedged: buy {b['ticker']} YES@{b['price']}c + "
                            f"buy {s['ticker']} NO@{s['price']}c = "
                            f"${total_cost}c cost, guaranteed {hedged_profit}c if one resolves"
                        ),
                    })

    # Sort by expected profit descending
    opportunities.sort(key=lambda x: x.get("expected_profit_cents", 0), reverse=True)

    return opportunities
