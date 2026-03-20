#!/usr/bin/env python3
"""
6-Hour Trading Digest — Comprehensive intelligence brief.
Runs every 6 hours. Collects all trade events, analyzes performance,
generates AI-powered brief with sentiment and improvements,
writes findings back to research_log.json, sends ONE Telegram message.
"""
import json, os, sys, time, requests, fcntl
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))

EVENTS_FILE = "/tmp/trade_events.json"
LEDGER_PATH = os.environ.get("PAPER_LEDGER", "/home/ubuntu/.openclaw/workspace/paper_trades.json")
RESEARCH_LOG = "/home/ubuntu/.openclaw/workspace/research_log.json"
DIGEST_LOG = "/home/ubuntu/.openclaw/workspace/digest_log.json"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(msg):
    """Send message, splitting if too long for Telegram's 4096 char limit."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    chunks = []
    while len(msg) > 4000:
        split = msg[:4000].rfind("\n")
        if split < 100:
            split = 4000
        chunks.append(msg[:split])
        msg = msg[split:]
    chunks.append(msg)
    for chunk in chunks:
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": chunk}, timeout=15)
            time.sleep(0.5)
        except:
            pass


def consume_events():
    """Read and clear all accumulated trade events."""
    try:
        if not os.path.exists(EVENTS_FILE):
            return []
        with open(EVENTS_FILE, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                events = json.load(f)
            except:
                events = []
            f.seek(0)
            f.truncate()
            json.dump([], f)
        return events
    except:
        return []


def load_ledger():
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except:
        return {"starting_equity": 500, "trades": []}


def load_research():
    try:
        with open(RESEARCH_LOG) as f:
            return json.load(f)
    except:
        return {"reports": []}


def load_digest_log():
    try:
        with open(DIGEST_LOG) as f:
            return json.load(f)
    except:
        return {"digests": [], "last_digest": None}


def save_digest_log(log):
    with open(DIGEST_LOG, "w") as f:
        json.dump(log, f, indent=2)


def get_balance():
    """Get Kalshi balance."""
    try:
        from kalshi_trade import api
        r = api("prod", "GET", "/portfolio/balance")
        if r.status_code == 200:
            b = r.json()
            return {
                "cash": b.get("balance", 0) / 100,
                "positions": b.get("portfolio_value", 0) / 100,
                "total": (b.get("balance", 0) + b.get("portfolio_value", 0)) / 100,
            }
    except:
        pass
    return {"cash": 0, "positions": 0, "total": 0}


def get_period_trades(hours=6):
    """Get trades from the last N hours."""
    ledger = load_ledger()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    period_trades = []
    for t in ledger["trades"]:
        opened = t.get("opened_at", t.get("opened", ""))
        closed = t.get("closed_at", "")
        if opened >= cutoff or (closed and closed >= cutoff):
            period_trades.append(t)
    return period_trades


def analyze_period(trades):
    """Analyze trades from the period."""
    opened = [t for t in trades if t["status"] == "OPEN"]
    closed = [t for t in trades if t["status"] != "OPEN" and t.get("pnl") is not None]

    if not closed:
        return {
            "total": len(trades), "opened": len(opened), "closed": 0,
            "wins": 0, "losses": 0, "pnl": 0, "win_rate": 0,
            "best": None, "worst": None, "by_series": {},
            "avg_conviction": 0,
        }

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    pnl = sum(t["pnl"] for t in closed)

    by_series = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0})
    for t in closed:
        series = t["ticker"].split("-")[0]
        by_series[series]["count"] += 1
        by_series[series]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            by_series[series]["wins"] += 1

    best = max(closed, key=lambda t: t["pnl"]) if closed else None
    worst = min(closed, key=lambda t: t["pnl"]) if closed else None

    return {
        "total": len(trades), "opened": len(opened), "closed": len(closed),
        "wins": len(wins), "losses": len(losses), "pnl": pnl,
        "win_rate": len(wins) / len(closed) if closed else 0,
        "best": {"ticker": best["ticker"][:35], "pnl": best["pnl"]} if best else None,
        "worst": {"ticker": worst["ticker"][:35], "pnl": worst["pnl"]} if worst else None,
        "by_series": dict(by_series),
    }


def extract_structured_brief(text):
    """Parse AI output to find the 5 structured sections, stripping thinking artifacts."""
    sections = ["SENTIMENT", "WHAT WORKED", "WHAT DIDN'T", "IMPROVEMENTS", "OUTLOOK"]
    markers = {s: None for s in sections}

    for s in sections:
        for pattern in [f"{s}:", f"{s} -", f"{s}\n", f"**{s}**", f"## {s}", f"{s.title()}:"]:
            pos = text.find(pattern)
            if pos >= 0:
                markers[s] = pos
                break

    found = {s: p for s, p in markers.items() if p is not None}
    if len(found) < 2:
        lines = text.split("\n")
        clean = [l for l in lines if not any(
            l.strip().lower().startswith(w) for w in [
                "i'm", "i am", "let me", "i need", "i want", "i think",
                "looking", "i notice", "however", "overall", "to summarize",
            ]
        )]
        return "\n".join(clean).strip()[:800] or text[:500]

    ordered = sorted(found.items(), key=lambda x: x[1])
    result_parts = []
    for i, (section, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(text)
        chunk = text[start:end].strip()
        lines = chunk.split("\n")
        clean_lines = []
        for line in lines:
            s = line.strip().lower()
            if any(s.startswith(w) for w in [
                "i'm", "i am", "let me", "i need", "i want", "i think",
                "looking at", "i notice", "however,", "overall,",
                "to summarize", "let's", "finding", "i should", "i hope",
                "it's exciting", "this could", "i could",
            ]):
                continue
            clean_lines.append(line)
        result_parts.append("\n".join(clean_lines).strip())

    return "\n\n".join(p for p in result_parts if p)


def generate_ai_brief(period_stats, balance, research, events_summary):
    """Use AI to generate a comprehensive trading brief."""
    from model_router import call_model

    research_data = research.get("reports", [])[-1] if research.get("reports") else {}
    hist_summary = research.get("historical_summary", "No historical data yet")

    data_block = (
        f"PERIOD: {period_stats['closed']} closed, {period_stats['opened']} open | "
        f"P&L ${period_stats['pnl']:.2f} | {period_stats['wins']}W/{period_stats['losses']}L "
        f"({period_stats['win_rate']:.0%} WR)\n"
        f"Best: {period_stats['best']} | Worst: {period_stats['worst']}\n"
        f"Markets: {json.dumps(period_stats['by_series'], default=str)[:300]}\n"
        f"Cash ${balance['cash']:.2f} | Total ${balance['total']:.2f}\n"
        f"Research: decay={research_data.get('edge_decay', {}).get('decay_detected', '?')}, "
        f"Sharpe={research_data.get('monte_carlo', {}).get('sharpe_approx', '?')}, "
        f"patterns={research_data.get('autopsy', {}).get('patterns', {})}\n"
        f"History: {hist_summary[:200]}\nEvents: {events_summary[:200]}"
    )

    prompt = (
        f"Given this trading data, write a brief with EXACTLY these 5 sections:\n\n"
        f"{data_block}\n\n"
        f"SENTIMENT: [1 sentence]\n"
        f"WHAT WORKED: [1-2 bullets]\n"
        f"WHAT DIDN'T: [1-2 bullets]\n"
        f"IMPROVEMENTS: [2-3 actionable changes]\n"
        f"OUTLOOK: [1 sentence]"
    )

    system = (
        "You are a trading analyst. Output ONLY the 5 sections requested. "
        "Start your response with SENTIMENT: — no preamble, no thinking, no commentary."
    )

    label, response = call_model(prompt, tier="digest", system=system, max_tokens=600)
    if not response:
        return "AI brief unavailable — model error."

    return extract_structured_brief(response)


def build_improvements(ai_brief, period_stats, research):
    """Extract improvements from AI brief and write them to research_log for the bot to read."""
    try:
        data = load_research()
        if not data.get("reports"):
            return

        latest = data["reports"][-1]
        latest["digest_improvements"] = ai_brief
        latest["digest_timestamp"] = datetime.now(timezone.utc).isoformat()
        latest["period_stats"] = {
            "pnl": period_stats["pnl"],
            "win_rate": period_stats["win_rate"],
            "wins": period_stats["wins"],
            "losses": period_stats["losses"],
        }

        with open(RESEARCH_LOG, "w") as f:
            json.dump(data, f, indent=2)
    except:
        pass


def main():
    ts = datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y-%m-%d %H:%M UTC")
    period_label = ts.strftime("%b %d %H:%M")

    events = consume_events()
    period_trades = get_period_trades(hours=6)
    stats = analyze_period(period_trades)
    balance = get_balance()
    research = load_research()

    events_summary = ""
    if events:
        btc15m_events = [e for e in events if e.get("source") == "btc15m"]
        crypto_events = [e for e in events if e.get("source") == "crypto"]
        auto_events = [e for e in events if e.get("source") == "auto_scan"]
        events_summary = (
            f"{len(btc15m_events)} BTC 15M events, "
            f"{len(crypto_events)} crypto events, "
            f"{len(auto_events)} auto_scan events"
        )

    # Run research agent for fresh data
    try:
        from research_agent import run_cycle
        run_cycle()
        research = load_research()
    except Exception as e:
        events_summary += f" | Research agent error: {str(e)[:50]}"

    ai_brief = generate_ai_brief(stats, balance, research, events_summary)
    build_improvements(ai_brief, stats, research)

    # Build the Telegram message
    pnl_emoji = "\U0001f4c8" if stats["pnl"] >= 0 else "\U0001f4c9"
    wr_str = f"{stats['win_rate']:.0%}" if stats["closed"] > 0 else "N/A"

    msg_lines = [
        f"\U0001f4cb 6-HOUR TRADING BRIEF — {period_label}",
        f"{'=' * 35}",
        "",
        f"{pnl_emoji} P&L: ${stats['pnl']:.2f} | {stats['wins']}W/{stats['losses']}L ({wr_str})",
        f"\U0001f4b0 Cash: ${balance['cash']:.2f} | Positions: ${balance['positions']:.2f} | Total: ${balance['total']:.2f}",
        f"\U0001f4ca Trades: {stats['closed']} closed, {stats['opened']} open",
    ]

    if stats["best"]:
        msg_lines.append(f"\U0001f3c6 Best: {stats['best']['ticker']} ({stats['best']['pnl']:+.2f})")
    if stats["worst"]:
        msg_lines.append(f"\u274c Worst: {stats['worst']['ticker']} ({stats['worst']['pnl']:+.2f})")

    if stats["by_series"]:
        msg_lines.append("")
        msg_lines.append("\U0001f4ca BY MARKET:")
        for series, data in sorted(stats["by_series"].items(), key=lambda x: -abs(x[1]["pnl"])):
            wr = data["wins"] / data["count"] if data["count"] > 0 else 0
            msg_lines.append(f"  {series}: {data['count']} trades, ${data['pnl']:.2f}, {wr:.0%} WR")

    if events:
        msg_lines.append("")
        msg_lines.append(f"\U0001f4e8 EVENTS: {len(events)} total")
        # Show last few trade executions/settlements
        trade_events = [e for e in events if any(kw in e.get("msg", "") for kw in ["EXECUTED", "Trade", "Win", "Loss", "NEW POSITION", "CLOSED"])]
        for evt in trade_events[-8:]:
            first_line = evt["msg"].split("\n")[0][:60]
            msg_lines.append(f"  \u2022 {first_line}")

    msg_lines.append("")
    msg_lines.append(f"{'=' * 35}")
    msg_lines.append(f"\U0001f9e0 AI ANALYSIS:")
    msg_lines.append(ai_brief)

    # Research stats footer
    r_data = research.get("reports", [])[-1] if research.get("reports") else {}
    decay = r_data.get("edge_decay", {}).get("decay_detected", "?")
    sharpe = r_data.get("monte_carlo", {}).get("sharpe_approx", 0)
    msg_lines.append("")
    msg_lines.append(f"\U0001f52c Research: decay={decay} | Sharpe={sharpe:.2f}")

    try:
        or_key = os.environ.get("OPENROUTER_API_KEY", "")
        if or_key:
            or_r = requests.get("https://openrouter.ai/api/v1/auth/key",
                                headers={"Authorization": f"Bearer {or_key}"}, timeout=5)
            if or_r.status_code == 200:
                od = or_r.json().get("data", {})
                msg_lines.append(f"\U0001f916 AI ${od.get('usage', 0):.2f} used | ${od.get('limit_remaining', 0):.2f} left")
    except:
        pass

    full_msg = "\n".join(msg_lines)
    send_telegram(full_msg)

    # Save digest to log
    digest_entry = {
        "timestamp": ts_str,
        "stats": stats,
        "balance": balance,
        "events_count": len(events),
        "ai_brief": ai_brief[:500],
    }
    dlog = load_digest_log()
    dlog["digests"].append(digest_entry)
    dlog["digests"] = dlog["digests"][-30:]  # keep last 30 digests
    dlog["last_digest"] = ts_str
    save_digest_log(dlog)

    print(f"Digest sent: {stats['closed']} trades, ${stats['pnl']:.2f} PnL, {len(events)} events")
    return digest_entry


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2, default=str))
