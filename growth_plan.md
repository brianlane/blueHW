Growth Strategy: From 
100
t
o
100to10K+
Phase 1: Foundation (Now → Week 2) — Capital: $100-500
Current state: 
99
K
a
l
s
h
i
,
99Kalshi,500 Webull. 8 open positions.

Compound winners aggressively: The new crypto yearly/monthly markets (KXBTCY, KXBTCMAX100) have massive volume (5.9M, 4.4M contracts) and tight 1c spreads. Low-cost entry at 2-6c with 10-50x upside.
Increase scan frequency for BTC 15M: Change crypto_scanner from */5 to */1 (every minute) since 15-minute markets need faster reactions.
Auto-reinvest: When a position hits target, immediately redeploy capital into next opportunity instead of sitting idle.
Risk per trade: Keep at 
1
(
100
c
)
p
e
r
K
a
l
s
h
i
t
r
a
d
e
,
1(100c)perKalshitrade,50 per stock trade.
Phase 2: Market Expansion (Weeks 2-4) — Capital: $500-2K
Add Webull Event Contracts: Trade the same Kalshi markets through Webull's event API. Different liquidity pools = more opportunity. Event contracts support Crypto (Mon-Fri 8AM-6PM), Index (8AM-4PM), and Sports (24/7).
Add Webull Futures: Start with micro futures (MES for S&P, MNQ for Nasdaq). These trade 23 hours/day, 5 days/week with massive leverage.
Add Economics scanner: Fed decisions (3.3M vol), CPI data (3.4M vol), gas prices (3.1M vol) — these are event-driven with predictable resolution dates.
Scale position sizing: Move from fixed $1 to Kelly-fraction based sizing (already partially implemented). Risk 2% of equity per trade.
Phase 3: Speed & Intelligence (Weeks 4-8) — Capital: $2K-10K
Websocket streaming: Replace polling (every 5 min) with Kalshi's websocket feed for real-time orderbook updates. React to opportunities in <1 second.
Market maker logic: Post both bid and ask on liquid markets, earning the spread. Kalshi's maker fee is lower than taker fee.
Cross-market arbitrage: BTC 15M market + Webull crypto spot = hedge. If BTC 15M says "BTC > 
83
K
i
n
15
m
i
n
"
a
t
3
c
,
a
n
d
B
T
C
i
s
a
t
83Kin15min"at3c,andBTCisat82,900 and rising, buy 15M YES + hedge with Webull crypto.
Smarter AI: Upgrade from qwen3.5:2b to gemini-2.5-flash for debate team. Better reasoning = better conviction scoring = larger positions on high-confidence trades.
Phase 4: Scale (Months 2-6) — Capital: $10K+
Politics/Entertainment markets: 224M + 22.6M volume. Add natural language understanding for political event prediction.
Multi-account: Open Kalshi FCM-cleared accounts for higher position limits.
Portfolio-level risk: Move from per-trade risk to portfolio-level VAR. Maintain 50+ diversified positions across all categories.
Speed advantage: Move to a VPS co-located near Kalshi's servers (AWS us-east) for <10ms order latency.
Key Metrics to Track
Metric	Target (Phase 1)	Target (Phase 4)
Win rate	>55%	>60%
Avg return per trade	2x cost	3x cost
Trades per day	5-10	50-100
Markets scanned	44 series	200+ series
Scan frequency	5 min	Real-time websocket
Capital deployed	$100	$10K+
Daily P&L target	$5-10	$200+