#!/usr/bin/env python3
"""
Guardian-Executor — The absolute safety layer.
Treats capital loss as literal death.

Responsibilities:
  - Monitor equity curve in real-time
  - Daily drawdown beyond limit → KILL (halt all trading)
  - Lifetime drawdown beyond limit → KILL (permanent halt)
  - Veto any trade that violates risk rules
  - Log every kill/veto in permanent audit trail
"""
import json, os, time, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

LEDGER_PATH = os.environ.get("PAPER_LEDGER", "/home/ubuntu/.openclaw/workspace/paper_trades.json")
GUARDIAN_LOG = "/home/ubuntu/.openclaw/workspace/guardian_log.json"
KILL_FILE = "/tmp/guardian_kill_active"
RESEARCH_LOG = "/home/ubuntu/.openclaw/workspace/research_log.json"

STARTING_CASH = 500.0
MAX_DAILY_DD = 0.08       # 8% = $40 daily loss limit on $500
MAX_LIFETIME_DD = 0.30    # 30% = $150 lifetime loss limit
MAX_RISK_PER_TRADE = 0.04 # 4% = $20 max per trade on $500
MAX_OPEN_POSITIONS = 25


def load_ledger():
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except:
        return {"starting_equity": STARTING_CASH, "trades": []}


def load_guardian_log():
    try:
        with open(GUARDIAN_LOG) as f:
            return json.load(f)
    except:
        return {"events": [], "daily_high": {}, "kills": 0}


def save_guardian_log(log):
    with open(GUARDIAN_LOG, "w") as f:
        json.dump(log, f, indent=2)


def load_research_insights():
    """Load latest research agent findings for adaptive risk management."""
    try:
        with open(RESEARCH_LOG) as f:
            data = json.load(f)
        reports = data.get("reports", [])
        if not reports:
            return {}
        latest = reports[-1]
        return {
            "edge_decay": latest.get("edge_decay", {}).get("decay_detected", False),
            "recent_win_rate": latest.get("edge_decay", {}).get("recent_win_rate", 0.5),
            "avg_max_dd": latest.get("monte_carlo", {}).get("avg_max_dd", 0),
            "sharpe": latest.get("monte_carlo", {}).get("sharpe_approx", 0),
            "worst_pattern": max(
                latest.get("autopsy", {}).get("patterns", {}).items(),
                key=lambda x: x[1], default=("none", 0)
            )[0],
            "compacted_count": data.get("compacted_count", 0),
        }
    except:
        return {}


def current_equity():
    """Return available CASH (not total equity) for position sizing."""
    try:
        from kalshi_trade import api
        r = api("prod", "GET", "/portfolio/balance")
        if r.status_code == 200:
            b = r.json()
            cash = b.get("balance", 0) / 100
            return cash if cash > 0 else 1.0
    except:
        pass
    return STARTING_CASH


def total_equity():
    """Return total portfolio value (cash + positions)."""
    try:
        from kalshi_trade import api
        r = api("prod", "GET", "/portfolio/balance")
        if r.status_code == 200:
            b = r.json()
            return (b.get("balance", 0) + b.get("portfolio_value", 0)) / 100
    except:
        pass
    return current_equity()


def daily_pnl():
    """Sum of P&L from trades closed today."""
    ledger = load_ledger()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(t.get("pnl", 0) for t in ledger["trades"]
               if t["status"] != "OPEN" and t.get("closed_at", "").startswith(today))


def is_killed():
    return os.path.exists(KILL_FILE)


def kill(reason):
    """Halt all trading immediately."""
    log = load_guardian_log()
    event = {
        "type": "KILL",
        "reason": reason,
        "equity": current_equity(),
        "total_equity": total_equity(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    log["events"].append(event)
    log["kills"] = log.get("kills", 0) + 1
    save_guardian_log(log)

    with open(KILL_FILE, "w") as f:
        f.write(json.dumps(event))

    try:
        import requests
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if token:
            msg = f"🚨 GUARDIAN KILL SWITCH\n{reason}\nCash: ${event['equity']:.2f} | Total: ${event['total_equity']:.2f}"
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": "7238485437", "text": msg}, timeout=10)
    except:
        pass

    return event


def check_health():
    """Run all Guardian checks. Returns (is_healthy, message)."""
    if is_killed():
        return False, "KILLED — trading halted. Manual review required."

    eq = current_equity()
    t_eq = total_equity()

    # Lifetime drawdown: compare total equity against starting deposit
    lifetime_dd = (STARTING_CASH - t_eq) / STARTING_CASH if t_eq < STARTING_CASH else 0

    log = load_guardian_log()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_high = log.get("daily_high", {})
    if today not in daily_high or eq > daily_high.get(today, 0):
        daily_high[today] = eq
        log["daily_high"] = daily_high
        save_guardian_log(log)

    peak_today = daily_high.get(today, eq)
    intraday_dd = (peak_today - eq) / peak_today if peak_today > 0 and eq < peak_today else 0

    # Daily drawdown kill: if we've lost more than 8% of today's peak
    if intraday_dd >= MAX_DAILY_DD:
        event = kill(f"Daily drawdown {intraday_dd:.1%} exceeds {MAX_DAILY_DD:.0%} limit. Peak=${peak_today:.2f}, Current=${eq:.2f}")
        return False, f"KILLED: {event['reason']}"

    # Lifetime drawdown kill: if total equity dropped 30%+ from deposit
    if lifetime_dd >= MAX_LIFETIME_DD:
        event = kill(f"Lifetime drawdown {lifetime_dd:.1%} exceeds {MAX_LIFETIME_DD:.0%} limit. Start=${STARTING_CASH:.2f}, Current=${t_eq:.2f}")
        return False, f"KILLED: {event['reason']}"

    return True, f"Healthy: cash=${eq:.2f}, total=${t_eq:.2f}, daily_dd={intraday_dd:.1%}, lifetime_dd={lifetime_dd:.1%}"


def veto_trade(proposal):
    """Check if a trade proposal should be vetoed. Returns (approved, reason)."""
    if is_killed():
        return False, "Trading halted by Guardian kill switch"

    eq = current_equity()

    # Calculate risk in dollars
    price = proposal.get("price", 0)
    qty = proposal.get("qty", 1)
    risk = price * qty / 100  # cents to dollars for Kalshi

    risk_pct = risk / eq if eq > 0 else 1

    # Adaptive risk: if research detected edge decay, tighten limits
    research = load_research_insights()
    effective_max_risk = MAX_RISK_PER_TRADE
    if research.get("edge_decay"):
        effective_max_risk = MAX_RISK_PER_TRADE * 0.5
    elif research.get("recent_win_rate", 0.5) > 0.4:
        effective_max_risk = min(MAX_RISK_PER_TRADE * 1.25, 0.06)

    if risk_pct > effective_max_risk:
        return False, f"Risk {risk_pct:.1%} exceeds {effective_max_risk:.0%} max (${risk:.2f} on ${eq:.2f} equity)"

    # Check open position concentration
    ledger = load_ledger()
    open_count = sum(1 for t in ledger["trades"] if t["status"] == "OPEN")
    if open_count >= MAX_OPEN_POSITIONS:
        return False, f"Too many open positions ({open_count})"

    return True, "Approved"


def revive():
    """Clear kill switch after manual review."""
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)
        log = load_guardian_log()
        log["events"].append({
            "type": "REVIVE",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": current_equity(),
        })
        save_guardian_log(log)
        return True
    return False


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "revive":
        if revive():
            print("Guardian kill switch cleared. Trading resumed.")
        else:
            print("No active kill to clear.")
    else:
        healthy, msg = check_health()
        print(f"Guardian: {msg}")
        print(f"  Cash: ${current_equity():.2f}")
        print(f"  Total: ${total_equity():.2f}")
        print(f"  Daily P&L: ${daily_pnl():.2f}")
        print(f"  Kills: {load_guardian_log().get('kills', 0)}")
        research = load_research_insights()
        if research:
            print(f"  Research: decay={research.get('edge_decay')}, win_rate={research.get('recent_win_rate','?')}")
