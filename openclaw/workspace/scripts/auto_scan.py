#!/usr/bin/env python3
"""
Auto-scanner v7 — Capped candidates, smarter R/R, silent rejections.
Only sends Telegram when trades EXECUTE or CLOSE (not rejection-only).
Caps debate candidates at 5 to avoid timeout cascades.
"""
import json, os, sys, time, subprocess, requests, re, fcntl
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

RESEARCH_LOG = "/home/ubuntu/.openclaw/workspace/research_log.json"
EVENTS_FILE = "/tmp/trade_events.json"
SCRIPTS = "/home/ubuntu/.openclaw/workspace/scripts"
LEDGER = os.path.join(SCRIPTS, "paper_ledger.py")
sys.path.insert(0, SCRIPTS)

MAX_RISK_PER_TRADE = 100
KELLY_FRACTION = 0.25
MAX_STOCK_RISK = 200  # max dollars of risk (loss) per stock trade
MAX_STOCK_POSITION = 2500  # max dollars of exposure per stock trade
MAX_SPREAD_CENTS = 6
MIN_OI = 50
MAX_HOURS_TO_CLOSE = 24  # Only trade markets closing within 24 hours
MAX_OPEN_POSITIONS = 20
MAX_DEBATE_CANDIDATES = 5
COOLDOWN_FILE = "/tmp/trade_cooldowns.json"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

TOP_STOCKS = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","TSLA","META","BRK-B","JPM","V",
    "UNH","MA","HD","PG","JNJ","ABBV","MRK","COST","CRM","NFLX",
    "ORCL","AMD","LLY","AVGO","PEP","KO","WMT","BAC","CSCO","TMO",
    "ADBE","MCD","ACN","DIS","INTC","QCOM","CMCSA","NKE","UPS","PM",
    "TXN","NEE","AMGN","BMY","PYPL","SQ","SHOP","COIN","PLTR","RIVN",
]
TOP_ETFS = [
    "SPY","QQQ","IWM","DIA","XLF","XLK","XLE","XLV","XLI","XLB",
    "XLU","XLP","XLC","XLRE","GLD","SLV","TLT","HYG","VXX","ARKK",
]
ALL_SYMBOLS = TOP_STOCKS + TOP_ETFS

CITY_MAP = {
    "KXHIGHNY":  {"name": "New York",  "lat": 40.7128, "lon": -74.0060},
    "KXHIGHMIA": {"name": "Miami",     "lat": 25.7617, "lon": -80.1918},
    "KXHIGHCHI": {"name": "Chicago",   "lat": 41.8781, "lon": -87.6298},
    "KXHIGHLA":  {"name": "Los Angeles","lat": 34.0522, "lon": -118.2437},
    "KXHIGHHOU": {"name": "Houston",   "lat": 29.7604, "lon": -95.3698},
    "KXHIGHDAL": {"name": "Dallas",    "lat": 32.7767, "lon": -96.7970},
}

SERIES_UNDERLYING = {
    "KXBTC": "BTC-USD", "KXBTCD": "BTC-USD", "KXBTCY": "BTC-USD",
    "KXBTCMAX": "BTC-USD", "KXBTCMIN": "BTC-USD",
    "KXETH": "ETH-USD", "KXETHD": "ETH-USD", "KXETHY": "ETH-USD",
    "KXETHMAX": "ETH-USD", "KXETHMIN": "ETH-USD",
    "KXSPY": "SPY", "KXNASDAQ": "QQQ", "KXNASDAQ100": "QQQ",
    "KXINX": "SPY", "KXINXU": "SPY", "KXINXY": "SPY",
    "KXNATGAS": "NG=F", "KXWTI": "CL=F", "KXWTIMAX": "CL=F",
    "KXSOLD": "GC=F", "KXGOLDMON": "GC=F",
    "KXSILVERMON": "SI=F", "KXCOPPERMON": "HG=F",
}

SERIES_LABELS = {
    "KXBTC": "Bitcoin", "KXBTCD": "Bitcoin Daily", "KXBTCY": "BTC Yearly",
    "KXBTCMAX100": "BTC >$100K", "KXBTCMAXY": "BTC Max Yearly",
    "KXBTCMAXMON": "BTC Max Monthly", "KXBTCMINMON": "BTC Min Monthly",
    "KXETH": "Ethereum", "KXETHD": "ETH Daily", "KXETHY": "ETH Yearly",
    "KXETHMAXY": "ETH Max Yearly", "KXETHMAXMON": "ETH Max Monthly",
    "KXSPY": "S&P 500", "KXNASDAQ": "Nasdaq", "KXNASDAQ100": "Nasdaq 100",
    "KXINX": "S&P 500", "KXINXU": "S&P 500", "KXINXY": "S&P 500 Yearly",
    "KXNASDAQ100Y": "Nasdaq Yearly",
    "KXNATGAS": "Nat Gas", "KXWTI": "WTI Oil", "KXWTIMAX": "Oil Max",
    "KXSOLD": "Gold", "KXGOLDMON": "Gold Monthly",
    "KXSILVERMON": "Silver Monthly", "KXCOPPERMON": "Copper Monthly",
    "KXHIGHNY": "New York", "KXHIGHMIA": "Miami", "KXHIGHCHI": "Chicago",
    "KXHIGHLA": "Los Angeles", "KXHIGHHOU": "Houston", "KXHIGHDAL": "Dallas",
    "KXRAINMIAM": "Rain Miami", "KXRAINNYCM": "Rain NYC", "KXRAINCHIM": "Rain Chicago",
    "KXNYCSNOWM": "Snow NYC", "KXCHISNOWM": "Snow Chicago",
    "KXFEDDECISION": "Fed Decision", "KXECONSTATCPIYOY": "CPI YoY",
    "KXAAAGASM": "Gas Prices", "KXRATECUTCOUNT": "Rate Cut Count",
    "KXCPI": "CPI", "KXCPIYOY": "CPI YoY", "KXPAYROLLS": "Payrolls",
}


def load_ledger():
    path = os.environ.get("PAPER_LEDGER", "/home/ubuntu/.openclaw/workspace/paper_trades.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"starting_equity": 10000.0, "trades": [], "positions": {}}

def get_open_tickers():
    return {t["ticker"] for t in load_ledger()["trades"] if t["status"] == "OPEN"}

def open_position_count():
    return sum(1 for t in load_ledger()["trades"] if t["status"] == "OPEN")

def run_cmd(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.strip()
    except:
        return ""

def d2c(val):
    try:
        return int(round(float(val or "0") * 100))
    except:
        return 0

def ptag(market):
    return "\U0001f3db Kalshi"


# ── Broad Market (v8/chart) ─────────────────────────────────────

_broad_cache = {"data": None, "ts": 0}

def _fetch_chart_price(item):
    sym, name = item
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=2d&interval=1d"
        r = requests.get(url, headers=YAHOO_HEADERS, timeout=8)
        if r.status_code == 200:
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("previousClose") or meta.get("chartPreviousClose") or 0
            chg = ((price - prev) / prev * 100) if prev > 0 else 0
            return sym, {"name": name, "price": price, "change_pct": round(chg, 1), "prev_close": prev}
    except:
        pass
    return sym, None

def get_broad_market():
    if _broad_cache["data"] and time.time() - _broad_cache["ts"] < 300:
        return _broad_cache["data"]
    syms = {
        "SPY": "S&P 500", "QQQ": "Nasdaq 100", "DIA": "Dow Jones",
        "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum",
        "GC=F": "Gold", "CL=F": "Oil (WTI)", "NG=F": "Natural Gas",
        "TLT": "Bonds (20yr)", "VIXY": "VIX (volatility)",
    }
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = dict(r for r in pool.map(_fetch_chart_price, syms.items()) if r[1])
        _broad_cache["data"] = results
        _broad_cache["ts"] = time.time()
        return results
    except:
        return {}

def get_underlying_context(series):
    for prefix, sym in SERIES_UNDERLYING.items():
        if series.startswith(prefix):
            market = get_broad_market()
            info = market.get(sym)
            if info:
                return f"{info['name']}: ${info['price']:,.2f} ({info['change_pct']:+.1f}% today)"
            break
    return ""

def build_market_summary():
    market = get_broad_market()
    if not market:
        return ""
    parts = []
    for sym, label in [("VIXY", "VIX"), ("SPY", "S&P"), ("BTC-USD", "BTC")]:
        info = market.get(sym)
        if info:
            parts.append(f"{label}: {info['change_pct']:+.1f}%")
    return " | ".join(parts)


# ── Cooldown ────────────────────────────────────────────────────

def load_cooldowns():
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_cooldowns(cd):
    cd = {k: v for k, v in cd.items() if v > time.time() - 14400}
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(cd, f)

def set_cooldown(ticker):
    cd = load_cooldowns()
    cd[ticker] = time.time()
    save_cooldowns(cd)

def is_on_cooldown(ticker):
    cd = load_cooldowns()
    return ticker in cd and time.time() - cd[ticker] < 3600


# ── NWS Weather ─────────────────────────────────────────────────

_forecast_cache = {}

def get_nws_forecast(city_key):
    if city_key in _forecast_cache:
        return _forecast_cache[city_key]
    city = CITY_MAP.get(city_key)
    if not city:
        return {}
    try:
        headers = {"User-Agent": "NemoClawBot/1.0 (trading@example.com)"}
        r = requests.get(f"https://api.weather.gov/points/{city['lat']},{city['lon']}",
                         headers=headers, timeout=10)
        if r.status_code != 200:
            return {}
        forecast_url = r.json().get("properties", {}).get("forecast", "")
        if not forecast_url:
            return {}
        r2 = requests.get(forecast_url, headers=headers, timeout=10)
        if r2.status_code != 200:
            return {}
        periods = r2.json().get("properties", {}).get("periods", [])
        result = {}
        for p in periods:
            if not p.get("isDaytime"):
                continue
            start = p.get("startTime", "")
            temp = p.get("temperature")
            if start and temp is not None:
                result[start[:10]] = int(temp)
        _forecast_cache[city_key] = result
        return result
    except:
        return {}

def parse_ticker_date(ticker):
    m = re.search(r'-(\d{2})([A-Z]{3})(\d{2})-', ticker)
    if not m:
        return None
    yy, mon_str, dd = m.groups()
    months = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
              "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
    return f"20{yy}-{months.get(mon_str, '01')}-{dd}"

def parse_ticker_threshold(ticker):
    m = re.search(r'-([TB])(\d+\.?\d*)$', ticker)
    if not m:
        return None, None
    return ("above" if m.group(1) == "T" else "bucket"), float(m.group(2))

def get_city_for_series(series):
    for key in CITY_MAP:
        if series.startswith(key):
            return key
    return None


# ── Stock news ──────────────────────────────────────────────────

def get_stock_news(symbol):
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={symbol}&newsCount=5&quotesCount=0"
        r = requests.get(url, headers=YAHOO_HEADERS, timeout=8)
        if r.status_code == 200:
            news = r.json().get("news", [])
            return "\n".join(f"  - {n.get('title', '')[:100]}" for n in news[:3] if n.get("title"))
    except:
        pass
    return ""


# ── Stock Quotes (v8/chart parallel) ────────────────────────────

def get_batch_quotes(symbols):
    quotes = {}
    def _fetch_one(sym):
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"
            r = requests.get(url, headers=YAHOO_HEADERS, timeout=10)
            if r.status_code != 200:
                return sym, None
            data = r.json().get("chart", {}).get("result", [])
            if not data:
                return sym, None
            meta = data[0].get("meta", {})
            indicators = data[0].get("indicators", {}).get("quote", [{}])[0]
            volumes = [v for v in (indicators.get("volume") or []) if v is not None]
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("previousClose") or meta.get("chartPreviousClose") or 0
            if not price or not prev:
                return sym, None
            chg_pct = ((price - prev) / prev * 100) if prev > 0 else 0
            vol = volumes[-1] if volumes else 0
            avg_vol = sum(volumes[-10:]) / min(len(volumes), 10) if len(volumes) >= 2 else 1
            name = meta.get("shortName") or meta.get("symbol", sym)
            return sym, {
                "price": float(price), "prev_close": float(prev),
                "change_pct": float(chg_pct),
                "volume": int(vol or 0), "avg_volume": int(avg_vol or 1),
                "name": name,
            }
        except:
            return sym, None
    with ThreadPoolExecutor(max_workers=15) as pool:
        for sym, q in pool.map(_fetch_one, symbols):
            if q:
                quotes[sym] = q
    return quotes


# ── Kalshi scanning ─────────────────────────────────────────────

def scan_kalshi():
    try:
        from kalshi_trade import api
    except:
        return []

    opps = []
    series_list = [
        # Weather highs (4 cities — removed KXHIGHNY/KXHIGHMIA per digest analysis: 0 wins, consistent losses)
        "KXHIGHCHI","KXHIGHLA","KXHIGHHOU","KXHIGHDAL",
        # Weather rain/snow (high volume)
        "KXRAINMIAM","KXRAINNYCM","KXRAINCHIM","KXNYCSNOWM","KXCHISNOWM",
        # Financials (S&P, Nasdaq, Oil, Gold, Silver, Copper)
        "KXINXY","KXNASDAQ100Y","KXWTIMAX","KXGOLDMON","KXSILVERMON","KXCOPPERMON",
        "KXINX","KXINXU","KXNASDAQ100","KXSPY","KXNASDAQ","KXNATGAS","KXWTI",
        # Economics (Fed, CPI, Gas prices)
        "KXFEDDECISION","KXECONSTATCPIYOY","KXAAAGASM","KXRATECUTCOUNT",
        "KXCPI","KXCPIYOY","KXPAYROLLS",
        # Crypto — daily only (15M handled by btc15m_scanner, no yearly/monthly)
        "KXBTCD","KXETHD","KXSOLD",
    ]

    for series in series_list:
        try:
            r = api("prod", "GET", f"/markets?series_ticker={series}&status=open&limit=10")
            if r.status_code != 200:
                continue
            city_key = get_city_for_series(series)
            forecast = get_nws_forecast(city_key) if city_key else {}
            city_name = CITY_MAP[city_key]["name"] if city_key else ""
            underlying_ctx = get_underlying_context(series)

            for m in r.json().get("markets", []):
                ticker = m.get("ticker", "")
                title = m.get("title", "").replace("**", "")
                subtitle = m.get("subtitle", "")
                yb, ya = d2c(m.get("yes_bid_dollars")), d2c(m.get("yes_ask_dollars"))
                nb, na = d2c(m.get("no_bid_dollars")), d2c(m.get("no_ask_dollars"))
                oi = int(float(m.get("open_interest_fp", "0") or "0"))
                ct = m.get("close_time", "")

                if yb == 0 and nb == 0:
                    continue
                spy = (ya - yb) if ya > 0 and yb > 0 else 999
                spn = (na - nb) if na > 0 and nb > 0 else 999

                hl = 999
                if ct:
                    try:
                        hl = (datetime.fromisoformat(ct.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() / 3600
                    except:
                        pass
                if hl > MAX_HOURS_TO_CLOSE or hl < 0.5:
                    continue

                readable = subtitle or title
                series_label = SERIES_LABELS.get(series, "")
                if series_label and series_label not in readable:
                    readable = f"{series_label}: {readable}"

                forecast_info = ""
                if city_key and forecast:
                    target_date = parse_ticker_date(ticker)
                    kind, threshold = parse_ticker_threshold(ticker)
                    if target_date and target_date in forecast and threshold is not None:
                        fcast_high = forecast[target_date]
                        forecast_info = f"NWS: {city_name} high {fcast_high}\u00b0F on {target_date}"
                        if kind == "above":
                            supports_yes = fcast_high >= threshold
                        elif kind == "bucket":
                            supports_yes = threshold <= fcast_high < threshold + 2
                        else:
                            supports_yes = None
                        if supports_yes is not None:
                            forecast_info += f" ({'supports' if supports_yes else 'contradicts'} YES)"

                if yb > 0 and spy <= MAX_SPREAD_CENTS and oi >= MIN_OI:
                    mid = (yb + ya) / 2 if ya > 0 else yb
                    if mid <= 35:
                        if forecast_info and "contradicts YES" in forecast_info:
                            continue
                        edge = max(5, int((50 - mid) * 0.3))
                        qty = max(1, min(int(MAX_RISK_PER_TRADE / max(ya, 1)), 50))
                        stop = max(1, yb - max(5, int(mid * 0.3)))
                        opps.append({
                            "ticker": ticker, "action": "buy", "side": "yes",
                            "price": ya, "qty": qty,
                            "stop": stop, "target": min(99, ya + edge),
                            "label": f"BUY YES {readable} @ {ya}c ({hl:.0f}h left)",
                            "spread": spy, "oi": oi, "market": "kalshi",
                            "forecast_info": forecast_info, "news_context": underlying_ctx,
                        })
                    elif mid >= 70:
                        if forecast_info and "supports YES" in forecast_info:
                            continue
                        edge = max(5, int((mid - 50) * 0.3))
                        cost = max(100 - yb, 1)
                        qty = max(1, min(int(MAX_RISK_PER_TRADE / max(cost, 1)), 50))
                        stop_val = min(99, ya + 5)
                        if stop_val <= yb:
                            continue
                        opps.append({
                            "ticker": ticker, "action": "sell", "side": "yes",
                            "price": yb, "qty": qty,
                            "stop": stop_val, "target": max(1, yb - edge),
                            "label": f"SELL YES {readable} @ {yb}c ({hl:.0f}h left)",
                            "spread": spy, "oi": oi, "market": "kalshi",
                            "forecast_info": forecast_info, "news_context": underlying_ctx,
                        })

                if nb > 0 and spn <= MAX_SPREAD_CENTS and oi >= MIN_OI:
                    mid_n = (nb + na) / 2 if na > 0 else nb
                    if mid_n <= 35:
                        if forecast_info and "supports YES" in forecast_info:
                            continue
                        edge = max(5, int((50 - mid_n) * 0.3))
                        qty = max(1, min(int(MAX_RISK_PER_TRADE / max(na, 1)), 50))
                        stop = max(1, nb - max(5, int(mid_n * 0.3)))
                        opps.append({
                            "ticker": ticker, "action": "buy", "side": "no",
                            "price": na, "qty": qty,
                            "stop": stop, "target": min(99, na + edge),
                            "label": f"BUY NO {readable} @ {na}c ({hl:.0f}h left)",
                            "spread": spn, "oi": oi, "market": "kalshi",
                            "forecast_info": forecast_info, "news_context": underlying_ctx,
                        })
        except:
            continue

    opps.sort(key=lambda x: (x["spread"], -x["oi"]))
    return opps[:5]


# ── Stock scanning (improved R/R: 5% stop, prev_close target) ──

def scan_stocks():
    quotes = get_batch_quotes(ALL_SYMBOLS)
    opps = []
    for sym, q in quotes.items():
        pct, price, name = q["change_pct"], q["price"], q["name"]
        vol_ratio = q["volume"] / max(q["avg_volume"], 1)
        if abs(pct) < 2.5 or vol_ratio < 0.8:
            continue
        target = round(q["prev_close"], 2)
        news = f"{name} ({sym}) {'down' if pct < 0 else 'up'} {abs(pct):.1f}% on {vol_ratio:.1f}x vol"
        if pct < -2.5:
            stop = round(price * 0.95, 2)
            rr = abs(target - price) / abs(price - stop) if abs(price - stop) > 0 else 0
            if rr < 1.0:
                continue
            headlines = get_stock_news(sym)
            opps.append({
                "ticker": sym, "action": "BUY", "side": "BUY",
                "price": round(price, 2), "qty": min(max(1, int(MAX_STOCK_RISK / max(abs(price - stop), 0.01))), max(1, int(MAX_STOCK_POSITION / price))),
                "stop": stop, "target": target,
                "label": f"BUY {name} ({sym}) @ ${price:.2f} \u2014 down {pct:.1f}%",
                "market": "kalshi",  # Webull discontinued
                "_rr": rr,
            })
        elif pct > 2.5:
            stop = round(price * 1.05, 2)
            rr = abs(target - price) / abs(price - stop) if abs(price - stop) > 0 else 0
            if rr < 1.0:
                continue
            headlines = get_stock_news(sym)
            opps.append({
                "ticker": sym, "action": "SELL", "side": "SELL",
                "price": round(price, 2), "qty": min(max(1, int(MAX_STOCK_RISK / max(abs(price - stop), 0.01))), max(1, int(MAX_STOCK_POSITION / price))),
                "stop": stop, "target": target,
                "label": f"SELL {name} ({sym}) @ ${price:.2f} \u2014 up {pct:.1f}%",
                "market": "kalshi",  # Webull discontinued
                "_rr": rr,
            })

    opps.sort(key=lambda x: -x.get("_rr", 0))
    return opps


# ── Execution (capped at 5 candidates) ──────────────────────────

def execute_trades(opportunities):
    open_tickers = get_open_tickers()
    n_open = open_position_count()
    executed, rejected = [], []

    # Count positions per ticker for the max-per-ticker check
    ticker_counts = {}
    for t in load_ledger()["trades"]:
        if t["status"] == "OPEN":
            tk = t["ticker"]
            ticker_counts[tk] = ticker_counts.get(tk, 0) + t["qty"]

    candidates = []
    for opp in opportunities:
        if n_open + len(candidates) >= MAX_OPEN_POSITIONS:
            break
        if len(candidates) >= MAX_DEBATE_CANDIDATES:
            break
        if is_on_cooldown(opp["ticker"]):
            continue
        max_contracts = 100 if opp.get("market") == "kalshi" else 50
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

    try:
        from debate_team import debate_batch
        vote_results = debate_batch(candidates)
    except:
        vote_results = {}

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
                # Check orderbook depth — skip if empty
                ob_r = kalshi_api("prod", "GET", f"/markets/{opp['ticker']}/orderbook")
                if ob_r.status_code == 200:
                    ob = ob_r.json().get("orderbook", ob_r.json())
                    side_key = "no" if opp.get("side") == "no" else "yes"
                    asks = ob.get(side_key, [])
                    if not asks:
                        rejected.append(f"\u274c {ptag(mkt)} {opp['label']} — empty orderbook")
                        continue
                order = kalshi_place(
                    "prod", opp["ticker"], opp["action"],
                    opp.get("side", "yes"), opp["qty"], int(opp["price"])
                )
                if not order:
                    rejected.append(f"\u274c {ptag(mkt)} {opp['label']} — Kalshi order rejected")
                    continue
                order_id = order.get("order_id", "")
                time.sleep(2)
                qr = kalshi_api("prod", "GET", f"/portfolio/orders/{order_id}")
                if qr.status_code == 200:
                    odata = qr.json().get("order", qr.json())
                    filled = int(float(odata.get("fill_count_fp", "0")))
                    status = odata.get("status", "")
                    if filled == 0:
                        kalshi_cancel("prod", order_id)
                        rejected.append(f"\u274c {ptag(mkt)} {opp['label']} — 0 filled, canceled")
                        continue
                    if filled < opp["qty"]:
                        kalshi_cancel("prod", order_id)
                        print(f"  Partial fill: {filled}/{opp['qty']} — canceled remainder")
                    opp["qty"] = filled
            except Exception as e:
                rejected.append(f"\u274c {ptag(mkt)} {opp['label']} — Kalshi: {str(e)[:60]}")
                continue
        elif mkt != "kalshi":
            continue  # Only Kalshi supported
        broker_side = opp.get("side", "") if mkt == "kalshi" else ""
        cmd = (
            f'python3 {LEDGER} open "{opp["ticker"]}" {opp["action"].upper()} {opp["price"]} '
            f'{opp["qty"]} {opp["stop"]} {opp["target"]} '
            f'"{opp.get("label","")[:120]}" {mkt} {broker_side} {order_id}'
        )
        run_cmd(cmd)
        set_cooldown(opp["ticker"])
        executed.append(f"{ptag(opp['market'])} {opp['qty']}x {opp['label']}{vote_note}")
        n_open += 1

    return executed, rejected


# ── Stop/target checks ─────────────────────────────────────────

def check_stops():
    closed = []
    ledger = load_ledger()
    kalshi_tickers, stock_tickers = {}, set()
    for t in ledger["trades"]:
        if t["status"] != "OPEN":
            continue
        if t["ticker"].startswith("KX"):
            kalshi_tickers[t["ticker"]] = t
        else:
            stock_tickers.add(t["ticker"])

    prices = {}
    if kalshi_tickers:
        try:
            from kalshi_trade import api
            for ticker in kalshi_tickers:
                r = api("prod", "GET", f"/markets/{ticker}")
                if r.status_code == 200:
                    m = r.json().get("market", {})
                    if m.get("status") in ("closed", "settled"):
                        prices[ticker] = 100 if m.get("result") == "yes" else 0
                    else:
                        prices[ticker] = d2c(m.get("yes_bid_dollars"))
        except:
            pass

    if stock_tickers:
        for sym, q in get_batch_quotes(list(stock_tickers)).items():
            prices[sym] = q["price"]

    for t in ledger["trades"]:
        if t["status"] != "OPEN":
            continue
        tk = t["ticker"]
        if tk not in prices or prices[tk] == 0:
            continue
        cur = prices[tk]
        hit = None

        if t["side"] == "BUY":
            if t.get("stop_loss") and cur <= t["stop_loss"]:
                hit = "STOP"
            elif t.get("target") and cur >= t["target"]:
                if tk.startswith("KX"):
                    try:
                        from kalshi_trade import api
                        r2 = api("prod", "GET", f"/markets/{tk}")
                        if r2.status_code == 200:
                            ct = r2.json().get("market", {}).get("close_time", "")
                            if ct:
                                remaining = (datetime.fromisoformat(ct.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() / 3600
                                if remaining < 2:
                                    continue
                    except:
                        pass
                hit = "TARGET"
        elif t["side"] == "SELL":
            if t.get("stop_loss") and cur >= t["stop_loss"]:
                hit = "STOP"
            elif t.get("target") and cur <= t["target"]:
                hit = "TARGET"

        if hit:
            broker_ok = False
            if tk.startswith("KX"):
                try:
                    from kalshi_trade import place_order as kalshi_place
                    close_action = "sell" if t["side"] == "BUY" else "buy"
                    broker_side = t.get("broker_side") or "yes"
                    result = kalshi_place("prod", tk, close_action, broker_side, t["qty"], int(cur))
                    broker_ok = result is not None
                except Exception as e:
                    print(f"Kalshi close error {tk}: {e}")
            else:
                try:
                    pass  # Webull discontinued
                    broker_ok = result is not None
                except Exception as e:
                    pass

            if not broker_ok:
                print(f"  Skipping ledger close for {tk} — broker order failed")
                continue
            run_cmd(f'python3 {LEDGER} close {t["id"]} {cur} "{hit}"')
            if hit == "STOP":
                set_cooldown(tk)
            platform = "\U0001f3db Kalshi"
            reason = t.get("reason", tk)
            outcome = "profit \u2705" if hit == "TARGET" else "loss"
            price_str = f"{cur}c" if tk.startswith("KX") else f"${cur:,.2f}"
            closed.append(f"{platform} {reason} \u2014 {hit} @ {price_str} ({outcome})")

    return closed


# ── Telegram ────────────────────────────────────────────────────

def append_event(event_msg, source="auto_scan"):
    """Write event to shared file for digest to pick up."""
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
            "source": source,
            "msg": event_msg,
        })
        events = events[-200:]
        with open(EVENTS_FILE, "w") as f:
            _fcntl.flock(f, _fcntl.LOCK_EX)
            json.dump(events, f)
    except:
        pass


def consume_trade_events():
    """Read and clear trade events written by btc15m_scanner and crypto_scanner."""
    import fcntl as _fcntl
    events = []
    try:
        if not os.path.exists(EVENTS_FILE):
            return []
        with open(EVENTS_FILE, "r+") as f:
            _fcntl.flock(f, _fcntl.LOCK_EX)
            try:
                events = json.load(f)
            except:
                events = []
            # Clear the file after consuming
            f.seek(0)
            f.truncate()
            json.dump([], f)
        return events
    except:
        return []


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": os.environ.get("TELEGRAM_CHAT_ID", "7238485437"), "text": message}, timeout=10)
    except:
        pass


# ── Main ────────────────────────────────────────────────────────

def main():
    lock_path = "/tmp/auto_scan.lock"
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    full_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_file = "/tmp/scan.log"

    closed = check_stops()
    kalshi_opps = scan_kalshi()
    # Only scan stocks during US market hours (9:30-16:00 ET = 14:30-21:00 UTC)
    utc_now = datetime.now(timezone.utc)
    utc_hour = utc_now.hour + utc_now.minute / 60
    market_open = 14.5 <= utc_hour < 21.0  # 9:30 AM - 4:00 PM ET
    stock_opps = []  # Webull discontinued
    # Interleave Kalshi and stock opps so both get debate slots
    all_opps = []
    ki, si = 0, 0
    while ki < len(kalshi_opps) or si < len(stock_opps):
        if ki < len(kalshi_opps):
            all_opps.append(kalshi_opps[ki]); ki += 1
        if si < len(stock_opps):
            all_opps.append(stock_opps[si]); si += 1
    executed, rejected = execute_trades(all_opps)

    # Only send Telegram if something actually HAPPENED (executed or closed)
    if executed or closed:
        mkt = build_market_summary()
        header = f"\U0001f4ca Live Trades \u2014 {ts}"
        if mkt:
            header += f"\n\U0001f30d {mkt}"
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
        # Webull discontinued
        pass

        bal_str = " | ".join(bal_parts) if bal_parts else ""
        or_str = ""
        try:
            or_key = os.environ.get("OPENROUTER_API_KEY", "")
            if or_key:
                or_r = requests.get("https://openrouter.ai/api/v1/auth/key",
                                     headers={"Authorization": f"Bearer {or_key}"}, timeout=5)
                if or_r.status_code == 200:
                    ord = or_r.json().get("data", {})
                    used = ord.get("usage", 0)
                    remaining = ord.get("limit_remaining", 0)
                    or_str = f"\n\U0001f916 AI ${used:.2f} used | ${remaining:.2f} left"
        except:
            pass
        # Include BTC 15M and crypto scanner events
        trade_events = consume_trade_events()
        if trade_events:
            tg_lines.append("\n\u26a1 BTC/CRYPTO EVENTS:")
            for evt in trade_events:
                msg = evt.get("msg", "")
                # Trim each event to essential info (remove footers that auto_scan adds itself)
                lines = msg.split("\n")
                trimmed = []
                for line in lines:
                    if any(skip in line for skip in ["\U0001f4c8", "\U0001f4b0", "\U0001f4b5", "\U0001f916", "\U0001f52c", "\u26a0"]):
                        continue
                    if line.strip():
                        trimmed.append(line)
                if trimmed:
                    tg_lines.append("  " + " | ".join(trimmed[:3]))

        footer = f"\n\U0001f4c8 {n_open} open | Total ${total_equity:,.2f}"
        if bal_str:
            footer += f"\n\U0001f4b0 {bal_str}"
        if kalshi_cash > 0:
            footer += f"\n\U0001f4b5 Cash available: ${kalshi_cash:.2f}"
        footer += or_str
        tg_lines.append(footer)
        # Write to shared events file — digest.py sends Telegram every 6 hours
        full_msg = "\n".join(tg_lines)
        if executed or closed or trade_events:
            append_event(full_msg, source="auto_scan")

    with open(log_file, "a") as f:
        f.write(f"{full_ts} exec={len(executed)} close={len(closed)} rej={len(rejected)} scan={len(all_opps)}\n")


if __name__ == "__main__":
    main()
