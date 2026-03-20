#!/usr/bin/env python3
"""
Internal Debate Team v3 — Tiered cost optimization + model rotation.

Architecture (user's "Value Strategy"):
  1. Data fetching = FREE (HTTP calls to Yahoo, NWS)
  2. Macro/Technical analysis = R1 / DSR1 (cheap deep reasoning)
  3. Sentiment = Gemini Flash (fast text analysis)
  4. Risk Guard = Nano (precise rule-following, Flash fallback)

Model rotation: if primary fails, falls back automatically.
"""
import json, os, sys, time, hashlib, requests, re
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))

CACHE_FILE = "/tmp/debate_cache.json"
COOLDOWN_HOURS = 2
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── Data Fetching (FREE — HTTP only) ────────────────────────────

_market_cache = {"data": None, "ts": 0}
_news_cache = {"data": None, "ts": 0}


def fetch_market_overview():
    if _market_cache["data"] and time.time() - _market_cache["ts"] < 300:
        return _market_cache["data"]
    syms = {
        "SPY": "S&P 500", "QQQ": "Nasdaq 100", "DIA": "Dow Jones",
        "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum",
        "GC=F": "Gold", "CL=F": "Oil (WTI)", "NG=F": "Natural Gas",
        "TLT": "Bonds (20yr)", "VIXY": "VIX (volatility)",
    }
    def _f(item):
        sym, name = item
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=2d&interval=1d"
            r = requests.get(url, headers=YAHOO_HEADERS, timeout=8)
            if r.status_code == 200:
                meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                price = meta.get("regularMarketPrice", 0)
                prev = meta.get("previousClose") or meta.get("chartPreviousClose") or 0
                chg = ((price - prev) / prev * 100) if prev > 0 else 0
                return sym, {"name": name, "price": price, "change_pct": round(chg, 1)}
        except:
            pass
        return sym, None
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = dict(r for r in pool.map(_f, syms.items()) if r[1])
        _market_cache["data"] = results
        _market_cache["ts"] = time.time()
        return results
    except:
        return {}


def fetch_general_news():
    if _news_cache["data"] and time.time() - _news_cache["ts"] < 600:
        return _news_cache["data"]
    try:
        url = "https://query2.finance.yahoo.com/v1/finance/search?q=stock%20market%20today&newsCount=8&quotesCount=0"
        r = requests.get(url, headers=YAHOO_HEADERS, timeout=8)
        if r.status_code == 200:
            news = r.json().get("news", [])
            data = [n.get("title", "")[:120] for n in news[:6] if n.get("title")]
            _news_cache["data"] = data
            _news_cache["ts"] = time.time()
            return data
    except:
        pass
    return []


def fetch_ticker_news(symbol):
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={symbol}&newsCount=5&quotesCount=0"
        r = requests.get(url, headers=YAHOO_HEADERS, timeout=8)
        if r.status_code == 200:
            news = r.json().get("news", [])
            return [n.get("title", "")[:120] for n in news[:5] if n.get("title")]
    except:
        pass
    return []


def compute_technicals(symbol):
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d"
        r = requests.get(url, headers=YAHOO_HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return None
        indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = [c for c in (indicators.get("close") or []) if c is not None]
        volumes = [v for v in (indicators.get("volume") or []) if v is not None]
        if len(closes) < 50:
            return None

        current = closes[-1]
        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-50:]) / 50

        changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        recent = changes[-14:]
        gains = [c for c in recent if c > 0]
        losses = [-c for c in recent if c < 0]
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0.001
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))

        window = closes[-20:]
        mean = sum(window) / 20
        std = (sum((x - mean) ** 2 for x in window) / 20) ** 0.5
        bb_upper = mean + 2 * std
        bb_lower = mean - 2 * std
        bb_pos = (current - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5

        high_52w = max(closes)
        low_52w = min(closes)
        range_pos = (current - low_52w) / (high_52w - low_52w) if high_52w != low_52w else 0.5

        vol_trend = 1.0
        if len(volumes) >= 20:
            recent_vol = sum(volumes[-5:]) / 5
            avg_vol = sum(volumes[-20:]) / 20
            vol_trend = recent_vol / avg_vol if avg_vol > 0 else 1.0

        mom_5d = (current / closes[-6] - 1) * 100 if len(closes) > 5 else 0
        mom_10d = (current / closes[-11] - 1) * 100 if len(closes) > 10 else 0
        mom_20d = (current / closes[-21] - 1) * 100 if len(closes) > 20 else 0

        return {
            "current": round(current, 2), "sma20": round(sma20, 2), "sma50": round(sma50, 2),
            "rsi": round(rsi, 1),
            "bb_upper": round(bb_upper, 2), "bb_lower": round(bb_lower, 2),
            "bb_position": round(bb_pos, 2),
            "range_52w": round(range_pos, 2), "vol_trend": round(vol_trend, 2),
            "mom_5d": round(mom_5d, 1), "mom_10d": round(mom_10d, 1), "mom_20d": round(mom_20d, 1),
        }
    except:
        return None


def get_account_risk_context():
    try:
        path = os.environ.get("PAPER_LEDGER", "/home/ubuntu/.openclaw/workspace/paper_trades.json")
        if os.path.exists(path):
            with open(path) as f:
                ledger = json.load(f)
            equity = ledger.get("starting_equity", 10000)
            open_trades = [t for t in ledger.get("trades", []) if t.get("status") == "OPEN"]
            for t in ledger.get("trades", []):
                if t["status"] != "OPEN":
                    equity += t.get("pnl", 0)
            sectors = {}
            ticker_counts = {}
            for t in open_trades:
                cat = "kalshi" if t.get("ticker", "").startswith("KX") else "stock"
                sectors[cat] = sectors.get(cat, 0) + 1
                tk = t.get("ticker", "")
                ticker_counts[tk] = ticker_counts.get(tk, 0) + t.get("qty", 1)
            return {
                "equity": round(equity, 2), "open_count": len(open_trades),
                "open_tickers": [t["ticker"] for t in open_trades],
                "ticker_counts": ticker_counts,
                "sector_exposure": sectors,
            }
    except:
        pass
    return {"equity": 10000, "open_count": 0, "open_tickers": [], "sector_exposure": {}}


# ── Role Definitions with Model Rotation ────────────────────────

ROLES = {
    "Macro": {
        "models": ["deepseek/deepseek-r1", "deepseek/deepseek-r1-distill-qwen-32b"],
        "max_tokens": 1500,
        "timeout": 60,
    },
    "Sentiment": {
        "models": ["google/gemini-2.5-flash", "mistralai/mistral-small-3.1-24b-instruct"],
        "max_tokens": 500,
        "timeout": 30,
    },
    "Technical": {
        "models": ["deepseek/deepseek-r1-distill-qwen-32b", "deepseek/deepseek-r1"],
        "max_tokens": 800,
        "timeout": 45,
    },
    "RiskGuard": {
        "models": ["openai/gpt-5.4-nano", "google/gemini-2.5-flash"],
        "max_tokens": 400,
        "timeout": 25,
    },
}


# ── Per-Role Prompt Builders ────────────────────────────────────

def build_macro_prompt(opp):
    market = fetch_market_overview()
    news = fetch_general_news()

    market_lines = [f"  {i['name']}: ${i['price']:,.2f} ({i['change_pct']:+.1f}%)" for i in market.values()]
    news_lines = [f"  - {h}" for h in news[:4]]

    platform = "Kalshi prediction market" if opp.get("market") == "kalshi" else "Stock/ETF"
    forecast = f"\nFORECAST: {opp['forecast_info']}" if opp.get("forecast_info") else ""

    return f"""Macro strategist: analyze whether the macro environment supports this {platform} trade.

MARKETS: {'; '.join(f"{i['name']} {i['change_pct']:+.1f}%" for i in market.values()) if market else '(unavailable)'}
HEADLINES: {' | '.join(news[:3]) if news else '(none)'}
{forecast}
TRADE: {opp.get('label', opp['ticker'])} | Entry={opp['price']} Stop={opp.get('stop','?')} Target={opp.get('target','?')}

Does VIX/index trend/sector rotation support this? Headlines create tailwinds or headwinds?
End with exactly VOTE: YES or VOTE: NO"""


def build_sentiment_prompt(opp):
    ticker = opp.get("ticker", "")
    ticker_news = fetch_ticker_news(ticker) if not ticker.startswith("KX") else []
    scanner_news = opp.get("news_headlines", "")
    market_data = opp.get("news_context", "")

    ticker_block = " | ".join(ticker_news[:3]) if ticker_news else "(none)"
    forecast = f" FORECAST: {opp['forecast_info']}" if opp.get("forecast_info") else ""

    return f"""Sentiment analyst for {opp.get('label', opp['ticker'])}.

HEADLINES: {ticker_block}
{f"SCANNER: {scanner_news}" if scanner_news else ""}
{f"DATA: {market_data}" if market_data else ""}{forecast}

Is sentiment bullish, bearish, or neutral? Any catalysts or shifts?
Keep analysis to 3-4 sentences, then end with exactly VOTE: YES or VOTE: NO"""


def build_technical_prompt(opp):
    ticker = opp.get("ticker", "")
    technicals = compute_technicals(ticker) if not ticker.startswith("KX") else None

    if technicals:
        above20 = "ABOVE" if technicals["current"] > technicals["sma20"] else "BELOW"
        above50 = "ABOVE" if technicals["current"] > technicals["sma50"] else "BELOW"
        rsi_l = "OVERBOUGHT" if technicals["rsi"] > 70 else "OVERSOLD" if technicals["rsi"] < 30 else "NEUTRAL"

        tech = f"""INDICATORS for {ticker}:
  ${technicals['current']} | SMA20=${technicals['sma20']}({above20}) SMA50=${technicals['sma50']}({above50})
  RSI={technicals['rsi']}({rsi_l}) | BB={technicals['bb_position']} | 52wk={technicals['range_52w']}
  Volume={technicals['vol_trend']}x avg | Momentum: 5d={technicals['mom_5d']:+.1f}% 10d={technicals['mom_10d']:+.1f}% 20d={technicals['mom_20d']:+.1f}%"""
    else:
        tech = f"Prediction market: {ticker} @ {opp['price']}c. {opp.get('forecast_info','')}"

    return f"""Technical analyst: evaluate this trade using computed indicators.

{tech}

TRADE: {opp.get('label', opp['ticker'])} | {opp.get('action','?')} Entry={opp['price']} Stop={opp.get('stop','?')} Target={opp.get('target','?')}

Analyze RSI, SMA structure, Bollinger position, volume, momentum alignment step by step.
End with exactly VOTE: YES or VOTE: NO"""


def build_risk_prompt(opp):
    account = get_account_risk_context()
    price = float(opp.get("price", 0))
    stop = float(opp.get("stop", 0))
    target = float(opp.get("target", 0))
    qty = int(opp.get("qty", 1))

    if opp.get("market") == "kalshi":
        risk_per_unit = abs(price - stop) if stop else price
        total_risk = risk_per_unit * qty / 100
        reward = abs(target - price) * qty / 100
    else:
        risk_per_unit = abs(price - stop) if stop else price * 0.03
        total_risk = risk_per_unit * qty
        reward = abs(target - price) * qty

    eq = account["equity"]
    risk_pct = (total_risk / eq * 100) if eq > 0 else 100
    rr_ratio = reward / total_risk if total_risk > 0 else 0
    ticker_ct = account.get("ticker_counts", {}).get(opp["ticker"], 0)
    total_pos = account["open_count"]
    MAX_CONTRACTS_PER_TICKER = 100

    risk_ok = risk_pct <= 1.0
    rr_ok = rr_ratio >= 1.5
    max_ct = MAX_CONTRACTS_PER_TICKER
    dup_ok = ticker_ct < max_ct
    pos_ok = total_pos < 20

    all_pass = risk_ok and rr_ok and dup_ok and pos_ok

    return f"""Risk check. Reply ONLY with VOTE: YES (all rules pass) or VOTE: NO (any rule fails).

Risk: ${total_risk:.2f} = {risk_pct:.2f}% of ${eq:.0f} equity. Max 1%. {"PASS" if risk_ok else "FAIL"}
R/R: {rr_ratio:.2f}:1. Min 1.5:1. {"PASS" if rr_ok else "FAIL"}
Contracts held for {opp['ticker']}: {ticker_ct}/{max_ct} max. {"PASS" if dup_ok else "FAIL (max reached)"}
Total positions: {total_pos}/20. {"PASS" if pos_ok else "FAIL"}

{"All rules PASS." if all_pass else "One or more rules FAIL."}
VOTE: {"YES" if all_pass else "NO"}"""


PROMPT_BUILDERS = {
    "Macro": build_macro_prompt,
    "Sentiment": build_sentiment_prompt,
    "Technical": build_technical_prompt,
    "RiskGuard": build_risk_prompt,
}


# ── Caching ─────────────────────────────────────────────────────

def _opp_hash(opp):
    key = f"{opp['ticker']}|{opp['action']}|{opp['side']}|{opp['price']}"
    return hashlib.md5(key.encode()).hexdigest()[:12]

def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except:
        return {}

def _save_cache(cache):
    cutoff = time.time() - 86400
    cache = {k: v for k, v in cache.items() if v.get("ts", 0) > cutoff}
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)

def _cached_result(opp):
    h = _opp_hash(opp)
    entry = _load_cache().get(h)
    if entry and time.time() - entry.get("ts", 0) < COOLDOWN_HOURS * 3600:
        return entry
    return None

def _store_result(opp, result):
    cache = _load_cache()
    cache[_opp_hash(opp)] = {"ts": time.time(), **result}
    _save_cache(cache)


# ── Vote Parsing (handles reasoning-only responses) ─────────────

def _parse_vote(text):
    if not text:
        return None
    upper = text.upper()
    m = re.search(r'VOTE:\s*(YES|NO)', upper)
    if m:
        return m.group(1)
    m = re.search(r'\*\*(YES|NO)\*\*', upper)
    if m:
        return m.group(1)
    tail = upper[-500:]
    if "YES" in tail and "NO" not in tail:
        return "YES"
    if "NO" in tail and "YES" not in tail:
        return "NO"
    yes_pos = tail.rfind("YES")
    no_pos = tail.rfind("NO")
    if yes_pos > no_pos:
        return "YES"
    if no_pos > yes_pos:
        return "NO"
    return None


# ── Model Calls with Rotation ───────────────────────────────────

def _call_role_model(role, prompt):
    config = ROLES[role]
    models = config["models"]
    max_tokens = config["max_tokens"]
    timeout = config["timeout"]
    api_key = os.environ.get("OPENROUTER_API_KEY", "")

    for model_id in models:
        try:
            body = {
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "NemoClaw-Debate",
                },
                json=body, timeout=timeout,
            )
            if r.status_code in (429, 502, 503):
                continue
            if r.status_code != 200:
                continue
            choices = r.json().get("choices") or []
            if not choices:
                continue
            msg = choices[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning") or "").strip()
            combined = content
            if reasoning and not content:
                combined = reasoning
            elif reasoning and content:
                combined = reasoning + "\n" + content
            if combined:
                return model_id, combined
        except:
            continue
    return None, None


# ── Main Debate ─────────────────────────────────────────────────

def debate(opp):
    cached = _cached_result(opp)
    if cached:
        return cached

    votes = {}
    reasoning_log = {}

    def run_role(role):
        builder = PROMPT_BUILDERS[role]
        prompt = builder(opp)
        model_id, response = _call_role_model(role, prompt)
        return role, model_id, response

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run_role, role) for role in ROLES]
        for f in as_completed(futures, timeout=75):
            try:
                role, model_id, response = f.result()
                parsed = _parse_vote(response)
                if parsed:
                    votes[role] = parsed
                    model_short = (model_id or "?").split("/")[-1][:25]
                    reasoning_log[role] = {
                        "model": model_short,
                        "vote": parsed,
                        "excerpt": (response or "")[-250:],
                    }
            except:
                pass

    yes_ct = sum(1 for v in votes.values() if v == "YES")
    no_ct = sum(1 for v in votes.values() if v == "NO")
    total = yes_ct + no_ct
    risk_veto = votes.get("RiskGuard") == "NO"

    if risk_veto:
        approved = False
        status = f"VETOED by RiskGuard {yes_ct}/{total}"
    elif total < 2:
        approved = False
        status = f"REJECTED (only {total} votes)"
    elif total <= 2 and yes_ct == total:
        approved = True
        status = f"APPROVED {yes_ct}/{total} (unanimous)"
    elif yes_ct >= total / 2 and yes_ct >= 2:
        approved = True
        status = f"APPROVED {yes_ct}/{total}"
    else:
        approved = False
        status = f"REJECTED {yes_ct}/{total}"

    vote_str = " ".join(
        f"{k}={'Y' if v == 'YES' else 'N'}({reasoning_log.get(k,{}).get('model','?')})"
        for k, v in votes.items()
    )
    summary = f"{status} [{vote_str}]"

    result = {
        "approved": approved, "yes": yes_ct, "no": no_ct,
        "total": total, "votes": votes, "summary": summary,
        "risk_veto": risk_veto, "reasoning": reasoning_log,
    }
    _store_result(opp, result)
    return result


def debate_batch(opportunities):
    results = {}
    for opp in opportunities:
        cached = _cached_result(opp)
        if cached:
            results[opp["ticker"]] = cached
        else:
            results[opp["ticker"]] = debate(opp)
    return results


if __name__ == "__main__":
    test = {
        "ticker": "AAPL", "action": "BUY", "side": "BUY",
        "price": 215.50, "qty": 1, "stop": 209.04, "target": 220.00,
        "label": "BUY Apple Inc (AAPL) @ $215.50 — down 3.2%",
        "market": "kalshi",
        "news_context": "Apple Inc (AAPL) down 3.2% on 1.5x avg volume",
    }
    print("Testing debate team v3...")
    r = debate(test)
    print(json.dumps(r, indent=2))
