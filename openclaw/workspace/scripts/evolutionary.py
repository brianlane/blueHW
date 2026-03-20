#!/usr/bin/env python3
"""
Evolutionary Tuning Agent — Long-term self-improvement.
Runs Sunday 3 AM. Generates strategy mutations, backtests, selects survivor.
"""
import json, os, sys, random, copy
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

def load_archived_summaries():
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

LEDGER_PATH = os.environ.get("PAPER_LEDGER", "/home/ubuntu/.openclaw/workspace/paper_trades.json")
CONFIG_FILE = "/home/ubuntu/.openclaw/workspace/scripts/strategy_config.json"
EVO_LOG = "/home/ubuntu/.openclaw/workspace/evolution_log.json"
RESEARCH_LOG = "/home/ubuntu/.openclaw/workspace/research_log.json"

DEFAULT_CONFIG = {
    "max_spread_cents": 6,
    "min_oi": 50,
    "max_hours_to_close": 36,
    "stock_pct_threshold": 2.0,
    "stock_vol_ratio": 0.7,
    "kelly_fraction": 0.25,
    "max_risk_per_trade": 100,
    "max_open_positions": 20,
    "kalshi_mid_low": 35,
    "kalshi_mid_high": 70,
}


def load_research():
    """Load research findings to guide mutations toward areas that need improvement."""
    try:
        with open(RESEARCH_LOG) as f:
            data = json.load(f)
        reports = data.get("reports", [])
        if not reports:
            return {}
        latest = reports[-1]
        return {
            "edge_decay": latest.get("edge_decay", {}).get("decay_detected", False),
            "sharpe": latest.get("monte_carlo", {}).get("sharpe_approx", 0),
            "lottery_losses": latest.get("autopsy", {}).get("patterns", {}).get("lottery_ticket_2c", 0),
            "tight_stop_losses": latest.get("autopsy", {}).get("patterns", {}).get("tight_stop", 0),
            "win_rate": latest.get("monte_carlo", {}).get("win_rate", 0),
        }
    except:
        return {}


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except:
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def load_ledger():
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except:
        return {"starting_equity": 10000.0, "trades": []}


def mutate_config(base, research=None):
    """Generate a research-guided mutated config."""
    m = copy.deepcopy(base)
    research = research or {}

    # Bias mutations toward parameters research identified as problematic
    weights = {k: 1 for k in m.keys()}
    if research.get("lottery_losses", 0) > 10:
        weights["kalshi_mid_low"] = 5
    if research.get("tight_stop_losses", 0) > 10:
        weights["max_spread_cents"] = 5
    if research.get("edge_decay"):
        weights["kelly_fraction"] = 5
        weights["max_open_positions"] = 3

    params = list(weights.keys())
    w = [weights.get(p, 1) for p in params]
    param = random.choices(params, weights=w, k=1)[0]

    val = m.get(param, 1)
    if isinstance(val, float):
        m[param] = round(val * random.uniform(0.7, 1.3), 4)
    elif isinstance(val, int):
        m[param] = max(1, int(val * random.uniform(0.7, 1.3)))
    return m, param


def backtest_config(config, trades):
    """Simulate trades with given config parameters."""
    closed = [t for t in trades if t["status"] != "OPEN" and t.get("pnl") is not None]
    if not closed:
        return {"equity": 10000, "max_dd": 0, "sharpe": 0, "expectancy": 0}

    equity = 10000.0
    max_eq = equity
    max_dd = 0
    pnls = []

    for t in closed:
        pnl = t["pnl"]
        # Apply config filters retroactively
        if t["ticker"].startswith("KX"):
            if t.get("entry_price", 0) > config.get("kalshi_mid_low", 35) and t.get("entry_price", 0) < config.get("kalshi_mid_high", 70):
                continue  # Would have been filtered
        else:
            # Stock filter
            pass

        equity += pnl
        pnls.append(pnl)
        max_eq = max(max_eq, equity)
        dd = (max_eq - equity) / max_eq if max_eq > 0 else 0
        max_dd = max(max_dd, dd)

    avg_pnl = sum(pnls) / max(1, len(pnls))
    std_pnl = (sum((p - avg_pnl)**2 for p in pnls) / max(1, len(pnls)))**0.5 if pnls else 1

    return {
        "equity": equity,
        "max_dd": max_dd,
        "sharpe": avg_pnl / max(0.01, std_pnl),
        "expectancy": avg_pnl,
        "n_trades": len(pnls),
    }


def run_evolution():
    """Generate 5 mutations, backtest each, select the best."""
    ts = datetime.now(timezone.utc).isoformat()
    base_config = load_config()
    ledger = load_ledger()
    trades = ledger.get("trades", [])

    base_result = backtest_config(base_config, trades)
    candidates = [("current", base_config, base_result)]

    research = load_research()
    for i in range(5):
        mutated, param = mutate_config(base_config, research)
        result = backtest_config(mutated, trades)
        candidates.append((f"mutation_{i+1}_{param}", mutated, result))

    # Select best by: lowest drawdown + highest expectancy
    candidates.sort(key=lambda x: (-x[2]["sharpe"], x[2]["max_dd"]))
    winner_name, winner_config, winner_result = candidates[0]

    # Only apply if better than current
    applied = False
    if winner_name != "current" and winner_result["sharpe"] > base_result["sharpe"]:
        save_config(winner_config)
        applied = True

    log_entry = {
        "timestamp": ts,
        "base_result": base_result,
        "winner": winner_name,
        "winner_result": winner_result,
        "applied": applied,
        "candidates": [(n, r) for n, _, r in candidates],
    }

    try:
        with open(EVO_LOG) as f:
            evo_log = json.load(f)
    except:
        evo_log = {"runs": []}
    evo_log["runs"].append(log_entry)
    with open(EVO_LOG, "w") as f:
        json.dump(evo_log, f, indent=2)

    # Telegram report
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        decay = research.get('edge_decay', '?')
        wr = research.get('win_rate', 0)
        sh = research.get('sharpe', 0)
        status = '✅ Applied new config' if applied else '⏸ Current config retained'
        msg = (
            f"🧬 Evolutionary Tuning — {ts[:16]}\n\n"
            f"📊 Current: Sharpe={base_result['sharpe']:.2f} DD={base_result['max_dd']:.1%}\n"
            f"🏆 Winner: {winner_name}\n"
            f"   Sharpe={winner_result['sharpe']:.2f} DD={winner_result['max_dd']:.1%}\n"
            f"{status}\n"
            f"🔬 Research: decay={decay}, WR={wr:.0%}, Sharpe={sh:.2f}"
        )
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": "7238485437", "text": msg}, timeout=10)
        except:
            pass

    return log_entry


if __name__ == "__main__":
    result = run_evolution()
    print(json.dumps(result, indent=2, default=str))
