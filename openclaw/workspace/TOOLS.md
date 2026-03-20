# TOOLS.md - Trading Bot Data Access

## Kalshi API (Prediction Markets)
Base URL: `https://api.elections.kalshi.com/trade-api/v2`

### Quick helper script (handles auth automatically):
```bash
# List open events
python3 ~/workspace/scripts/kalshi_api.py events 20

# Get markets for an event
python3 ~/workspace/scripts/kalshi_api.py markets KXNEWPOPE-70

# Get single market details
python3 ~/workspace/scripts/kalshi_api.py market MARKET_TICKER

# Get orderbook
python3 ~/workspace/scripts/kalshi_api.py orderbook MARKET_TICKER

# Check account balance (authenticated)
python3 ~/workspace/scripts/kalshi_api.py balance

# Login test
python3 ~/workspace/scripts/kalshi_api.py login
```

### Direct curl (public, no auth):
```bash
curl -sS "https://api.elections.kalshi.com/trade-api/v2/events?limit=20&status=open" | python3 -m json.tool
curl -sS "https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=TICKER&limit=20" | python3 -m json.tool
curl -sS "https://api.elections.kalshi.com/trade-api/v2/markets/TICKER/orderbook" | python3 -m json.tool
```

## Stock Data (yfinance --  API was rejected)
```bash
python3 -c "
import yfinance as yf
for s in ['AAPL','TSLA','NVDA','SPY','QQQ','AMZN','GOOGL','META']:
    t = yf.Ticker(s)
    p = t.fast_info.last_price
    print(f'{s}: \${p:.2f}')
"
```

For detailed history:
```python
import yfinance as yf
ticker = yf.Ticker('AAPL')
hist = ticker.history(period='5d', interval='5m')
print(hist.tail(20))
```

## Browser (Lightpanda Cloud Chrome via CDP)
Headless Chrome available via Lightpanda Cloud. Use browser tools for sites
that need JavaScript rendering. Note: some sites (Kalshi, ) have bot
detection -- prefer API access when available.

## Memory
Write daily logs to `memory/YYYY-MM-DD.md`. Search with memory tools.

## Key Rules
- Always pipe curl JSON through `python3 -m json.tool` for readability
- Use the kalshi_api.py helper for authenticated Kalshi endpoints
- For stocks: yfinance is the primary source ( API access was denied)
- Save all analysis results to memory files for permanent record
- SOUL.md rules are absolute -- never violate risk parameters

## News Headlines (RSS)
Fetch headlines from major news sources - no API keys needed:
```
python3 scripts/news.py                          # all sources, 5 headlines each
python3 scripts/news.py "oil" "bbc,reuters" 3    # topic filter, specific sources, 3 items
```
Sources: reuters, bbc, cnbc, ap, marketwatch

## Paper Trading Ledger
Record and track paper trades:
```
python3 scripts/paper_ledger.py open <TICKER> <BUY|SELL> <PRICE> <QTY> <STOP_LOSS> <TARGET> "<REASON>"
python3 scripts/paper_ledger.py close <TRADE_ID> <EXIT_PRICE> "<REASON>"
python3 scripts/paper_ledger.py summary    # portfolio overview
python3 scripts/paper_ledger.py positions  # open positions JSON
python3 scripts/paper_ledger.py json       # full ledger
```
