#!/usr/bin/env python3
"""
Research Agent — Continuous learning and validation engine.
Runs at 8 AM and 8 PM automatically, plus on demand.

Workflow:
  1. RECALL: Pull every losing trade from history (oldest first)
  2. VALIDATE: Run Monte Carlo simulation on recent performance
  3. DISCOVER: Check news, edge decay, regime shifts
  4. PROPOSE: Generate improvements with proof they prevent past losses
  5. REPORT: Send findings to Telegram
"""
import json, os, sys, time, random, requests
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

LEDGER_PATH = os.environ.get("PAPER_LEDGER", "/home/ubuntu/.openclaw/workspace/paper_trades.json")
RESEARCH_LOG = "/home/ubuntu/.openclaw/workspace/research_log.json"


def load_ledger():
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except:
        return {"starting_equity": 10000.0, "trades": []}


def load_research_log():
    try:
        with open(RESEARCH_LOG) as f:
            return json.load(f)
    except:
        return {"reports": [], "proposals": [], "last_run": None}


def save_research_log(log):
    with open(RESEARCH_LOG, "w") as f:
        json.dump(log, f, indent=2)


def load_archived_summaries():
    """Load all agent-generated session summaries for historical reference."""
    summaries_dir = os.path.expanduser("~/.openclaw/archives/summaries")
    if not os.path.isdir(summaries_dir):
        return []
    results = []
    import glob as _glob
    for md in sorted(_glob.glob(f"{summaries_dir}/*.md")):
        try:
            with open(md) as f:
                results.append({"file": os.path.basename(md), "content": f.read()})
        except:
            continue
    return results


# ── Phase 1: RECALL — Autopsy of every loss ────────────────────

def recall_losses():
    """Pull every losing trade, analyze patterns. Also loads archived summaries."""
    archived = load_archived_summaries()
    if archived:
        print(f"  Loaded {len(archived)} archived session summaries for context")
    ledger = load_ledger()
    losses = [t for t in ledger["trades"] if t["status"] != "OPEN" and t.get("pnl", 0) < 0]
    losses.sort(key=lambda t: t.get("opened", ""))

    autopsy = {
        "total_losses": len(losses),
        "total_loss_amount": sum(t.get("pnl", 0) for t in losses),
        "patterns": defaultdict(int),
        "repeat_tickers": defaultdict(int),
        "worst_losses": [],
        "common_reasons": defaultdict(int),
    }

    for t in losses:
        ticker = t["ticker"]
        autopsy["repeat_tickers"][ticker] += 1
        reason = t.get("reason", "")

        if "cheap YES" in reason or "@ 2c" in reason or "@ 1c" in reason:
            autopsy["patterns"]["lottery_ticket_2c"] += 1
        if t.get("stop_loss") and t.get("entry_price") and abs(t["stop_loss"] - t["entry_price"]) < 2:
            autopsy["patterns"]["tight_stop"] += 1
        if t.get("close_reason") == "STOP" or t.get("close_reason") == "STOP_HIT":
            autopsy["patterns"]["stopped_out"] += 1

        close_reason = t.get("close_reason", "unknown")
        autopsy["common_reasons"][close_reason] += 1

    # Top 5 worst losses
    worst = sorted(losses, key=lambda t: t.get("pnl", 0))[:5]
    for t in worst:
        autopsy["worst_losses"].append({
            "ticker": t["ticker"],
            "pnl": t.get("pnl", 0),
            "reason": t.get("reason", "")[:100],
            "entry": t.get("entry_price", 0),
            "exit": t.get("exit_price", 0),
        })

    # Most-repeated losing tickers
    autopsy["repeat_tickers"] = dict(sorted(
        autopsy["repeat_tickers"].items(), key=lambda x: -x[1])[:5])
    autopsy["patterns"] = dict(autopsy["patterns"])
    autopsy["common_reasons"] = dict(autopsy["common_reasons"])

    return autopsy


# ── Phase 2: VALIDATE — Monte Carlo simulation ─────────────────

def monte_carlo_backtest(n_sims=1000):
    """Run Monte Carlo simulation on historical trade results."""
    ledger = load_ledger()
    closed = [t for t in ledger["trades"] if t["status"] != "OPEN" and t.get("pnl") is not None]
    if len(closed) < 5:
        return {"error": "Not enough closed trades for simulation"}

    pnls = [t["pnl"] for t in closed]
    starting = ledger.get("starting_equity", 10000)

    results = []
    for _ in range(n_sims):
        equity = starting
        max_eq = equity
        max_dd = 0
        sample = random.choices(pnls, k=len(pnls))
        for pnl in sample:
            equity += pnl
            max_eq = max(max_eq, equity)
            dd = (max_eq - equity) / max_eq if max_eq > 0 else 0
            max_dd = max(max_dd, dd)
        results.append({"final_equity": equity, "max_drawdown": max_dd})

    equities = [r["final_equity"] for r in results]
    drawdowns = [r["max_drawdown"] for r in results]

    return {
        "n_sims": n_sims,
        "n_trades": len(closed),
        "avg_equity": sum(equities) / len(equities),
        "median_equity": sorted(equities)[len(equities) // 2],
        "p5_equity": sorted(equities)[int(0.05 * len(equities))],
        "p95_equity": sorted(equities)[int(0.95 * len(equities))],
        "avg_max_dd": sum(drawdowns) / len(drawdowns),
        "worst_dd": max(drawdowns),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls),
        "avg_win": sum(p for p in pnls if p > 0) / max(1, sum(1 for p in pnls if p > 0)),
        "avg_loss": sum(p for p in pnls if p < 0) / max(1, sum(1 for p in pnls if p < 0)),
        "expectancy": sum(pnls) / len(pnls),
        "sharpe_approx": (sum(pnls) / len(pnls)) / (max(0.01, (sum((p - sum(pnls)/len(pnls))**2 for p in pnls) / len(pnls))**0.5)),
    }


# ── Phase 3: DISCOVER — Edge decay + news ──────────────────────

def check_edge_decay():
    """Compare recent performance vs historical to detect edge decay."""
    ledger = load_ledger()
    closed = [t for t in ledger["trades"] if t["status"] != "OPEN" and t.get("pnl") is not None]
    if len(closed) < 10:
        return {"status": "insufficient_data"}

    recent = closed[-10:]
    older = closed[:-10] if len(closed) > 10 else closed

    recent_wr = sum(1 for t in recent if t["pnl"] > 0) / len(recent)
    older_wr = sum(1 for t in older if t["pnl"] > 0) / max(1, len(older))
    recent_exp = sum(t["pnl"] for t in recent) / len(recent)
    older_exp = sum(t["pnl"] for t in older) / max(1, len(older))

    decay_detected = recent_wr < older_wr * 0.7 or recent_exp < older_exp * 0.5

    return {
        "recent_win_rate": recent_wr,
        "historical_win_rate": older_wr,
        "recent_expectancy": recent_exp,
        "historical_expectancy": older_exp,
        "decay_detected": decay_detected,
    }


# ── Phase 4: PROPOSE — AI-generated improvements ───────────────

def generate_proposals(autopsy, mc_results, edge_decay):
    """Use reasoning model to analyze findings and propose improvements."""
    from model_router import call_model

    # Load historical summary from compacted reports
    log = load_research_log()
    hist_summary = log.get("historical_summary", "No historical data yet")
    compacted_count = log.get("compacted_count", 0)

    prompt = f"""You are a trading research analyst. Analyze this performance data and propose specific improvements.

HISTORICAL CONTEXT ({compacted_count} prior reports compacted):
{hist_summary}

LOSS AUTOPSY:
- Total losses: {autopsy['total_losses']} trades, ${autopsy['total_loss_amount']:.2f}
- Loss patterns: {json.dumps(autopsy['patterns'])}
- Most repeated losing tickers: {json.dumps(autopsy['repeat_tickers'])}
- Worst losses: {json.dumps(autopsy['worst_losses'], indent=2)}

MONTE CARLO SIMULATION ({mc_results.get('n_sims', 0)} sims, {mc_results.get('n_trades', 0)} trades):
- Win rate: {mc_results.get('win_rate', 0):.1%}
- Avg win: ${mc_results.get('avg_win', 0):.2f}, Avg loss: ${mc_results.get('avg_loss', 0):.2f}
- Expectancy: ${mc_results.get('expectancy', 0):.2f}/trade
- Sharpe approx: {mc_results.get('sharpe_approx', 0):.2f}
- Avg max drawdown: {mc_results.get('avg_max_dd', 0):.1%}

EDGE DECAY:
- Recent win rate: {edge_decay.get('recent_win_rate', 0):.1%} vs historical {edge_decay.get('historical_win_rate', 0):.1%}
- Decay detected: {edge_decay.get('decay_detected', False)}

Propose 3 specific, actionable improvements that would:
1. Prevent the most common loss pattern
2. Improve the Sharpe ratio above 1.5
3. Reduce max drawdown below 15%

Be concise. Each proposal should be one sentence."""

    label, response = call_model(prompt, tier="digest", system="You are a concise trading analyst. Write clean, structured output. No thinking out loud.", max_tokens=400)
    return response or "No proposals generated"


# ── Phase 5: REPORT — Send findings to Telegram ────────────────

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": "7238485437", "text": message}, timeout=10)
    except:
        pass


# ── Main cycle ──────────────────────────────────────────────────

def update_strategy_config(autopsy, mc, decay):
    """Update strategy_config.json based on research findings."""
    try:
        cfg_path = os.path.join(os.path.dirname(__file__), "strategy_config.json")
        with open(cfg_path) as f:
            cfg = json.load(f)

        # If lottery ticket losses dominate, raise minimum price threshold
        lottery_count = autopsy.get("patterns", {}).get("lottery_ticket_2c", 0)
        if lottery_count > 15:
            cfg["kalshi_mid_low"] = max(cfg.get("kalshi_mid_low", 35), 20)

        # If tight stops are losing, widen the spread tolerance
        tight_count = autopsy.get("patterns", {}).get("tight_stop", 0)
        if tight_count > 10:
            cfg["max_spread_cents"] = max(cfg.get("max_spread_cents", 6), 8)

        # If edge is decaying, tighten position limits
        if decay.get("decay_detected"):
            cfg["max_open_positions"] = min(cfg.get("max_open_positions", 20), 15)
        else:
            cfg["max_open_positions"] = 20

        # If Sharpe is low, reduce Kelly fraction
        sharpe = mc.get("sharpe_approx", 0)
        if sharpe < 0.5:
            cfg["kelly_fraction"] = 0.15
        elif sharpe > 1.0:
            cfg["kelly_fraction"] = 0.30
        else:
            cfg["kelly_fraction"] = 0.25

        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"  Strategy config updated: mid_low={cfg['kalshi_mid_low']}, spread={cfg['max_spread_cents']}, kelly={cfg['kelly_fraction']}")
    except Exception as e:
        print(f"  Strategy config update error: {e}")




# ── Phase 6: COMPACT — Summarize old data to prevent unbounded growth ───

MAX_FULL_REPORTS = 5      # keep last 5 reports in full detail
MAX_FULL_TRADES = 200     # keep last 200 trades in full detail in paper_trades

def compact_research_log(log):
    """Summarize old reports using AI, keep only recent ones in full."""
    reports = log.get("reports", [])
    if len(reports) <= MAX_FULL_REPORTS:
        return log

    old_reports = reports[:-MAX_FULL_REPORTS]
    recent_reports = reports[-MAX_FULL_REPORTS:]

    # Build a summary of old reports using AI
    old_summary_data = []
    for r in old_reports:
        old_summary_data.append({
            "timestamp": r.get("timestamp", "?"),
            "losses": r.get("autopsy", {}).get("total_losses", 0),
            "loss_amount": r.get("autopsy", {}).get("total_loss_amount", 0),
            "win_rate": r.get("monte_carlo", {}).get("win_rate", 0),
            "sharpe": r.get("monte_carlo", {}).get("sharpe_approx", 0),
            "expectancy": r.get("monte_carlo", {}).get("expectancy", 0),
            "edge_decay": r.get("edge_decay", {}).get("decay_detected", False),
            "top_pattern": max(
                r.get("autopsy", {}).get("patterns", {}).items(),
                key=lambda x: x[1], default=("none", 0)
            )[0],
        })

    try:
        from model_router import call_model
        prompt = f"""Summarize these {len(old_summary_data)} trading research reports into a concise historical summary (max 500 chars).
Focus on: trends in win rate, edge decay, dominant loss patterns, and whether performance improved or declined.

Reports (oldest first):
{json.dumps(old_summary_data, indent=2)}

Previous historical summary: {log.get('historical_summary', 'None yet')}

Write a single paragraph combining all historical context."""

        label, summary = call_model(prompt, tier="cheap", max_tokens=200)
        if summary:
            log["historical_summary"] = summary
            print(f"  Compacted {len(old_reports)} old reports into AI summary ({len(summary)} chars)")
    except Exception as e:
        # Fallback: generate a simple stats-based summary without AI
        total_losses = sum(r["losses"] for r in old_summary_data)
        avg_wr = sum(r["win_rate"] for r in old_summary_data) / max(len(old_summary_data), 1)
        avg_sharpe = sum(r["sharpe"] for r in old_summary_data) / max(len(old_summary_data), 1)
        decay_count = sum(1 for r in old_summary_data if r["edge_decay"])
        log["historical_summary"] = (
            f"Compacted {len(old_reports)} reports ({old_summary_data[0]['timestamp']} to "
            f"{old_summary_data[-1]['timestamp']}): avg WR={avg_wr:.0%}, avg Sharpe={avg_sharpe:.2f}, "
            f"edge decay detected {decay_count}x, total losses analyzed={total_losses}"
        )
        print(f"  Compacted {len(old_reports)} reports (stats-only fallback)")

    # Also keep aggregate stats from old reports
    log["compacted_count"] = log.get("compacted_count", 0) + len(old_reports)
    log["reports"] = recent_reports
    return log


def compact_trades_ledger():
    """Summarize old closed trades, keep only recent ones in full detail."""
    try:
        with open(LEDGER_PATH) as f:
            ledger = json.load(f)
    except:
        return

    trades = ledger.get("trades", [])
    if len(trades) <= MAX_FULL_TRADES:
        return

    open_trades = [t for t in trades if t["status"] == "OPEN"]
    closed_trades = [t for t in trades if t["status"] != "OPEN"]

    if len(closed_trades) <= MAX_FULL_TRADES:
        return

    old_closed = closed_trades[:-MAX_FULL_TRADES]
    recent_closed = closed_trades[-MAX_FULL_TRADES:]

    # Aggregate old trades into summary stats
    old_wins = sum(1 for t in old_closed if t.get("pnl", 0) > 0)
    old_losses = sum(1 for t in old_closed if t.get("pnl", 0) < 0)
    old_pnl = sum(t.get("pnl", 0) for t in old_closed)
    old_tickers = {}
    for t in old_closed:
        series = t["ticker"].split("-")[0] if "-" in t["ticker"] else t["ticker"]
        old_tickers[series] = old_tickers.get(series, 0) + 1

    # Build AI summary
    try:
        from model_router import call_model
        prompt = f"""Summarize these {len(old_closed)} old trades into a concise historical note (max 300 chars).
Stats: {old_wins}W/{old_losses}L, PnL={old_pnl:+.2f}, top tickers: {dict(sorted(old_tickers.items(), key=lambda x:-x[1])[:5])}
Previous summary: {ledger.get('compacted_summary', 'None')}"""

        label, summary = call_model(prompt, tier="cheap", max_tokens=150)
        if summary:
            ledger["compacted_summary"] = summary
    except:
        ledger["compacted_summary"] = (
            f"Compacted {len(old_closed)} trades: {old_wins}W/{old_losses}L PnL={old_pnl:+.2f}"
        )

    ledger["compacted_trades_count"] = ledger.get("compacted_trades_count", 0) + len(old_closed)
    ledger["compacted_pnl"] = ledger.get("compacted_pnl", 0) + old_pnl
    ledger["trades"] = open_trades + recent_closed

    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)

    print(f"  Compacted {len(old_closed)} old trades (kept {len(recent_closed)} recent + {len(open_trades)} open)")



def scan_bot_farming(api_func=None):
    """
    Advanced HFT/bot activity analysis for BTC 15M markets.

    Most of the ~$230k daily volume is driven by AI agents + HFT firms doing
    latency arbitrage, micro-order stuffing, and orderbook sniping.
    Kalshi fights manipulation with 60-second RTI averaging at expiry.

    Our approach:
    1. DISTINGUISH real HFT liquidity from manipulation (no false positives on high volume)
    2. EXPLOIT predictable HFT patterns:
       - HFT makes prices accurate → respect tight-spread markets
       - Order stuffing creates mispricings → fade them
       - HFT exits 1-2 min before expiry → opportunity window
       - Volume clustering indicates smart money direction

    Returns dict of tickers with analysis:
        {ticker: {
            "type": "legitimate_hft" | "manipulation" | "mixed",
            "severity": "low" | "medium" | "high",
            "hft_quality_score": 0-100,
            "exploitable_patterns": [...],
            "defense_action": "trust_pricing" | "widen_edge" | "avoid" | "fade_stuffing",
            "reason": str,
        }}
    """
    if api_func is None:
        from kalshi_trade import authenticated_request
        api_func = authenticated_request

    analysis = {}

    try:
        open_r = api_func("prod", "GET", "/markets?series_ticker=KXBTC15M&status=open&limit=15")
        if open_r.status_code != 200:
            return analysis
        open_markets = open_r.json().get("markets", [])

        settled_r = api_func("prod", "GET", "/markets?series_ticker=KXBTC15M&status=settled&limit=10")
        settled_markets = settled_r.json().get("markets", []) if settled_r.status_code == 200 else []
        all_markets = open_markets + settled_markets

        # Build a volume baseline from settled markets for comparison
        settled_volumes = []
        for m in settled_markets:
            v = int(float(m.get("volume_fp", "0") or "0"))
            if v > 0:
                settled_volumes.append(v)
        avg_volume = sum(settled_volumes) / len(settled_volumes) if settled_volumes else 200
        vol_std = (sum((v - avg_volume)**2 for v in settled_volumes) / max(len(settled_volumes), 1)) ** 0.5 if len(settled_volumes) > 1 else avg_volume * 0.5

        for market in open_markets:
            ticker = market.get("ticker", "?")
            volume = int(float(market.get("volume_fp", "0") or "0"))
            oi = int(float(market.get("open_interest_fp", "0") or "0"))

            try:
                ob_r = api_func("prod", "GET", f"/markets/{ticker}/orderbook")
                if ob_r.status_code != 200:
                    continue
                ob = ob_r.json().get("orderbook", ob_r.json())
            except:
                continue

            yes_orders = ob.get("yes", [])
            no_orders = ob.get("no", [])
            yes_depth = sum(int(float(o.get("count_fp", "0") or "0")) for o in yes_orders)
            no_depth = sum(int(float(o.get("count_fp", "0") or "0")) for o in no_orders)
            total_depth = yes_depth + no_depth
            n_yes_levels = len(yes_orders)
            n_no_levels = len(no_orders)
            total_levels = n_yes_levels + n_no_levels

            # --- Spread analysis ---
            best_yes_bid = max((int(round(float(o.get("price_fp", "0") or "0") * 100)) for o in yes_orders), default=0)
            best_no_bid = max((int(round(float(o.get("price_fp", "0") or "0") * 100)) for o in no_orders), default=0)
            spread = max(0, 100 - best_yes_bid - best_no_bid) if best_yes_bid and best_no_bid else 99

            # --- HFT Quality Score (0-100) ---
            # Higher = more legitimate, efficient market making
            quality = 50

            # Tight spread = strong market making
            if spread <= 2:
                quality += 25
            elif spread <= 4:
                quality += 15
            elif spread >= 10:
                quality -= 15

            # Depth balance (both sides served = legitimate)
            if total_depth > 0:
                balance = min(yes_depth, no_depth) / max(yes_depth, no_depth, 1)
                quality += int(balance * 15)  # 0-15 points for balance

            # Multiple price levels = real market making (not single-level sniping)
            if total_levels >= 6:
                quality += 10
            elif total_levels <= 2:
                quality -= 10

            # Volume within normal range (not suspicious)
            vol_zscore = (volume - avg_volume) / max(vol_std, 1) if vol_std > 0 else 0
            if abs(vol_zscore) < 1.5:
                quality += 5
            elif vol_zscore > 3:
                quality -= 10  # abnormally high

            quality = max(0, min(100, quality))

            # --- Classify activity type ---
            exploitable = []
            reasons = []

            # Pattern 1: ORDER STUFFING — lots of small orders creating false depth
            small_order_count = sum(1 for o in yes_orders + no_orders
                                    if int(float(o.get("count_fp", "0") or "0")) <= 2)
            stuffing_ratio = small_order_count / max(total_levels, 1)
            is_stuffing = stuffing_ratio > 0.7 and total_levels >= 6

            # Pattern 2: WASH TRADING — high vol relative to OI
            vol_oi_ratio = volume / max(oi, 1) if oi > 0 else 0
            is_wash = vol_oi_ratio > 10 and volume > avg_volume * 2

            # Pattern 3: LATENCY ARBITRAGE — tight spread + high volume = HFT doing arb
            is_latency_arb = spread <= 3 and volume > avg_volume * 1.5

            # Pattern 4: ONE-SIDED PRESSURE — imbalance suggests directional HFT
            imbalance = abs(yes_depth - no_depth) / max(total_depth, 1)
            is_directional = imbalance > 0.6 and total_depth > 20

            # --- Determine type and action ---
            if is_wash:
                activity_type = "manipulation"
                severity = "high"
                action = "avoid"
                reasons.append(f"Wash trading: vol/OI={vol_oi_ratio:.0f}x, vol {vol_zscore:+.1f}σ above mean")
            elif is_stuffing and not is_latency_arb:
                activity_type = "manipulation"
                severity = "medium"
                action = "fade_stuffing"
                stuff_side = "YES" if yes_depth > no_depth else "NO"
                real_side = "NO" if stuff_side == "YES" else "YES"
                reasons.append(f"Order stuffing: {stuffing_ratio:.0%} small orders on {stuff_side} side")
                exploitable.append({
                    "pattern": "fade_stuffing",
                    "detail": f"Stuffing on {stuff_side} → fake depth → consider {real_side}",
                    "confidence": 0.6,
                })
            elif is_latency_arb:
                activity_type = "legitimate_hft"
                severity = "low"
                action = "trust_pricing"
                reasons.append(f"Latency arb: spread={spread}c, vol={volume} ({vol_zscore:+.1f}σ)")
                if is_directional:
                    flow_side = "YES" if yes_depth > no_depth else "NO"
                    exploitable.append({
                        "pattern": "hft_flow_direction",
                        "detail": f"HFT flow favoring {flow_side} ({imbalance:.0%} imbalance)",
                        "confidence": min(0.7, imbalance),
                    })
            elif is_directional:
                activity_type = "mixed"
                severity = "low"
                action = "widen_edge"
                flow_side = "YES" if yes_depth > no_depth else "NO"
                reasons.append(f"Directional pressure: {flow_side} depth {imbalance:.0%}")
                exploitable.append({
                    "pattern": "smart_money_flow",
                    "detail": f"Smart money likely on {flow_side} side",
                    "confidence": min(0.6, imbalance * 0.8),
                })
            else:
                activity_type = "legitimate_hft"
                severity = "low"
                action = "trust_pricing"
                reasons.append(f"Normal market: spread={spread}c, vol={volume}, depth={total_depth}")

            # Exploitable: HFT exit window (last 2 min)
            close_time = market.get("close_time", "")
            if close_time:
                try:
                    from datetime import datetime, timezone
                    ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    mins_left = (ct - datetime.now(timezone.utc)).total_seconds() / 60
                    if 1.0 <= mins_left <= 2.5:
                        exploitable.append({
                            "pattern": "hft_exit_window",
                            "detail": f"HFT pulling liquidity at {mins_left:.1f}m — spreads may widen",
                            "confidence": 0.5,
                        })
                except:
                    pass

            analysis[ticker] = {
                "type": activity_type,
                "severity": severity,
                "hft_quality_score": quality,
                "exploitable_patterns": exploitable,
                "defense_action": action,
                "reason": " | ".join(reasons),
                "volume": volume,
                "spread": spread,
                "depth_balance": round(balance, 2) if total_depth > 0 else 0,
                "imbalance": round(imbalance, 2),
            }

    except Exception as e:
        analysis["_error"] = {"reason": str(e), "severity": "low", "type": "error",
                              "hft_quality_score": 0, "exploitable_patterns": [],
                              "defense_action": "trust_pricing"}

    return analysis

def scan_copy_trading_signals(api_func=None):
    """
    SOUL: Copy-Trading Correlation Filter.
    Scan for smart-money patterns by analyzing:
      - Large order imbalances in the orderbook
      - Significant price movements that suggest informed trading
      - Volume surges that precede price moves
    Returns list of signals with correlation to our own analysis.
    """
    signals = []
    try:
        if api_func is None:
            from kalshi_trade import api as api_func

        r = api_func("prod", "GET", "/markets?series_ticker=KXBTC15M&status=open&limit=5")
        if r.status_code != 200:
            return signals

        for m in r.json().get("markets", []):
            ticker = m.get("ticker", "")
            ya = float(m.get("yes_ask_dollars", "0") or "0")
            yb = float(m.get("yes_bid_dollars", "0") or "0")
            vol = int(float(m.get("volume_fp", "0") or "0"))

            if vol < 100:
                continue

            # Check orderbook for large order imbalances
            try:
                ob_r = api_func("prod", "GET", f"/markets/{ticker}/orderbook")
                if ob_r.status_code == 200:
                    ob = ob_r.json().get("orderbook", ob_r.json())
                    yes_depth = sum(int(float(o.get("count_fp", "0") or "0"))
                                    for o in ob.get("yes", []))
                    no_depth = sum(int(float(o.get("count_fp", "0") or "0"))
                                   for o in ob.get("no", []))

                    total = yes_depth + no_depth
                    if total > 20:
                        yes_ratio = yes_depth / total
                        if yes_ratio > 0.70:
                            signals.append({
                                "ticker": ticker,
                                "type": "smart_money_yes",
                                "detail": f"YES depth {yes_ratio:.0%} of {total} contracts",
                                "confidence": "medium",
                            })
                        elif yes_ratio < 0.30:
                            signals.append({
                                "ticker": ticker,
                                "type": "smart_money_no",
                                "detail": f"NO depth {1-yes_ratio:.0%} of {total} contracts",
                                "confidence": "medium",
                            })
            except:
                pass

    except Exception as e:
        signals.append({"ticker": "_error", "type": "error", "detail": str(e)[:80]})

    return signals



def run_cycle():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log = load_research_log()

    autopsy = recall_losses()
    mc = monte_carlo_backtest()
    decay = check_edge_decay()

    # SOUL: Bot farming defense + copy-trading correlation
    bot_farming = scan_bot_farming()
    copy_signals = scan_copy_trading_signals()

    proposals = generate_proposals(autopsy, mc, decay)

    report = {
        "timestamp": ts,
        "autopsy": autopsy,
        "monte_carlo": mc,
        "edge_decay": decay,
        "proposals": proposals,
        "bot_farming": bot_farming,
        "copy_signals": copy_signals,
    }
    # Apply findings to strategy config
    update_strategy_config(autopsy, mc, decay)

    log["reports"].append(report)
    log["last_run"] = ts
    save_research_log(log)

    # Telegram summary
    lines = [f"🔬 Research Agent — {ts}"]
    lines.append(f"\n📉 Loss Autopsy: {autopsy['total_losses']} losses (${autopsy['total_loss_amount']:.2f})")
    if autopsy["patterns"]:
        lines.append(f"  Patterns: {', '.join(f'{k}={v}' for k,v in autopsy['patterns'].items())}")
    lines.append(f"\n📊 Monte Carlo ({mc.get('n_sims', 0)} sims):")
    lines.append(f"  Win rate: {mc.get('win_rate', 0):.1%} | Sharpe: {mc.get('sharpe_approx', 0):.2f}")
    lines.append(f"  Expectancy: ${mc.get('expectancy', 0):.2f}/trade")
    lines.append(f"  Avg drawdown: {mc.get('avg_max_dd', 0):.1%}")

    if decay.get("decay_detected"):
        lines.append(f"\n⚠️ EDGE DECAY DETECTED: WR {decay['recent_win_rate']:.0%} vs {decay['historical_win_rate']:.0%}")

    if bot_farming:
        manipulation = {k: v for k, v in bot_farming.items()
                        if isinstance(v, dict) and v.get("type") == "manipulation"}
        exploitable = {k: v for k, v in bot_farming.items()
                       if isinstance(v, dict) and v.get("exploitable_patterns")}
        legitimate = {k: v for k, v in bot_farming.items()
                      if isinstance(v, dict) and v.get("type") == "legitimate_hft"}

        if manipulation:
            lines.append(f"\n🚫 Manipulation detected: {len(manipulation)} markets")
            for tk, info in list(manipulation.items())[:3]:
                lines.append(f"  {tk}: {info.get('reason', '?')[:70]}")

        if exploitable:
            n_pat = sum(len(v.get("exploitable_patterns", [])) for v in exploitable.values())
            lines.append(f"\n🎯 Exploitable HFT: {n_pat} patterns in {len(exploitable)} markets")
            for tk, info in list(exploitable.items())[:3]:
                for ep in info.get("exploitable_patterns", [])[:1]:
                    lines.append(f"  {tk}: {ep.get('pattern')}: {ep.get('detail', '')[:50]}")

        if legitimate:
            avg_q = sum(v.get("hft_quality_score", 0) for v in legitimate.values()) / max(len(legitimate), 1)
            lines.append(f"\n✅ HFT markets: {len(legitimate)} clean (avg quality: {avg_q:.0f}/100)")

    if copy_signals:
        lines.append(f"\n📡 Smart Money: {len(copy_signals)} signals")
        for sig in copy_signals[:3]:
            lines.append(f"  {sig['ticker']}: {sig['detail']}")

    lines.append(f"\n💡 Proposals:\n{proposals}")
    send_telegram("\n".join(lines))

    # Compact old data to prevent unbounded file growth
    compact_research_log(log)
    save_research_log(log)
    compact_trades_ledger()

    return report


if __name__ == "__main__":
    report = run_cycle()
    print(json.dumps(report, indent=2, default=str))
