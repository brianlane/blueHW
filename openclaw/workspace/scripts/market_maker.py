#!/usr/bin/env python3
"""
Market Maker — Direction-neutral spread capture for BTC 15M markets.

Instead of predicting direction, we:
1. Post limit orders on BOTH sides of the book (buy YES low, sell YES high)
2. Capture the bid-ask spread regardless of outcome
3. Manage inventory so we don't accumulate too much on one side
4. Exit positions early when spread compresses (don't hold to binary expiry)
5. Pull liquidity near market close (HFT exit window)

This is the "unsexy winner" — 78-85% win rate, 1-3% monthly, low drawdown.
Combined with directional trades only when edge is massive.

API: Uses kalshi_trade.place_order for limit orders.
"""
import json, os, sys, time, math
from datetime import datetime, timezone

sys.path.insert(0, "/home/ubuntu/.openclaw/workspace/scripts")

INVENTORY_FILE = "/tmp/mm_inventory.json"
LOG_FILE = "/tmp/market_maker.log"

# ─── Strategy Parameters ───
MIN_SPREAD_CENTS = 3      # Don't MM if spread is less than 3c (HFT already tight)
MAX_SPREAD_CENTS = 20     # Don't MM if spread is too wide (illiquid/manipulated)
MIN_VOLUME = 50           # Minimum market volume to participate
MIN_MINUTES_LEFT = 4      # Don't start MM with < 4 minutes
MAX_MINUTES_LEFT = 13     # Don't MM brand-new markets (still forming)
PULL_LIQUIDITY_MINS = 1.5 # Pull all orders with < 1.5 min left (RTI risk)

# Position sizing
MAX_INVENTORY_PER_SIDE = 5    # Max contracts held on one side
MAX_TOTAL_INVENTORY = 8       # Max total contracts across all positions
QUOTE_SIZE = 2                # Contracts per quote
MAX_CAPITAL_PCT = 0.03        # Use max 3% of cash for MM inventory

# Spread management
EDGE_BUFFER_CENTS = 1     # How far inside the spread we quote
INVENTORY_SKEW_CENTS = 2  # Skew quotes away from accumulated inventory

# Profit targets
MIN_PROFIT_CENTS = 2      # Minimum profit per round trip after fees
FLATTEN_PROFIT_CENTS = 4  # Take profit and flatten if we've made this per contract


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts} {msg}\n")


def load_inventory():
    try:
        with open(INVENTORY_FILE) as f:
            data = json.load(f)
        cutoff = time.time() - 1200  # expire entries older than 20 min
        cleaned = {k: v for k, v in data.items() if v.get("ts", 0) > cutoff}
        return cleaned
    except:
        return {}


def save_inventory(data):
    with open(INVENTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_market_data(ticker, api_func):
    """Fetch market + orderbook data for MM decisions."""
    try:
        mr = api_func("prod", "GET", f"/markets/{ticker}")
        if mr.status_code != 200:
            return None
        md = mr.json().get("market", mr.json())

        obr = api_func("prod", "GET", f"/markets/{ticker}/orderbook")
        ob = obr.json().get("orderbook", obr.json()) if obr.status_code == 200 else {}

        yes_orders = ob.get("yes", [])
        no_orders = ob.get("no", [])

        best_yes_bid = max((int(round(float(o.get("price_fp", "0") or "0") * 100))
                           for o in yes_orders), default=0)
        best_no_bid = max((int(round(float(o.get("price_fp", "0") or "0") * 100))
                          for o in no_orders), default=0)

        # yes_ask = 100 - best_no_bid, no_ask = 100 - best_yes_bid
        yes_ask = 100 - best_no_bid if best_no_bid > 0 else 0
        no_ask = 100 - best_yes_bid if best_yes_bid > 0 else 0

        spread = yes_ask - best_yes_bid if (yes_ask > 0 and best_yes_bid > 0) else 99

        close_time = md.get("close_time", "")
        minutes_left = 0
        try:
            ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            minutes_left = (ct - datetime.now(timezone.utc)).total_seconds() / 60
        except:
            pass

        volume = int(float(md.get("volume_fp", "0") or "0"))

        yes_depth = sum(int(float(o.get("count_fp", "0") or "0")) for o in yes_orders)
        no_depth = sum(int(float(o.get("count_fp", "0") or "0")) for o in no_orders)

        return {
            "ticker": ticker,
            "best_yes_bid": best_yes_bid,
            "yes_ask": yes_ask,
            "best_no_bid": best_no_bid,
            "no_ask": no_ask,
            "spread": spread,
            "mid": (best_yes_bid + yes_ask) / 2 if best_yes_bid > 0 and yes_ask > 0 else 50,
            "minutes_left": minutes_left,
            "volume": volume,
            "yes_depth": yes_depth,
            "no_depth": no_depth,
            "status": md.get("status", ""),
        }
    except Exception as e:
        _log(f"get_market_data error {ticker}: {e}")
        return None


def should_mm(mdata):
    """Decide if this market is suitable for market making."""
    if not mdata:
        return False, "no data"
    if mdata["status"] != "active":
        return False, f"status={mdata['status']}"
    if mdata["minutes_left"] < MIN_MINUTES_LEFT:
        return False, f"too close to expiry ({mdata['minutes_left']:.1f}m)"
    if mdata["minutes_left"] > MAX_MINUTES_LEFT:
        return False, f"too new ({mdata['minutes_left']:.1f}m)"
    if mdata["volume"] < MIN_VOLUME:
        return False, f"low volume ({mdata['volume']})"
    if mdata["spread"] < MIN_SPREAD_CENTS:
        return False, f"spread too tight ({mdata['spread']}c) — HFT dominated"
    if mdata["spread"] > MAX_SPREAD_CENTS:
        return False, f"spread too wide ({mdata['spread']}c) — illiquid"
    return True, "ok"


def calculate_quotes(mdata, inventory):
    """
    Calculate bid/ask prices with inventory-aware skew.

    We quote inside the current spread, but skew away from our inventory.
    If we're long YES, we lower our YES bid (less eager to buy more)
    and lower our YES ask (more eager to sell).
    """
    ticker = mdata["ticker"]
    inv = inventory.get(ticker, {"yes_qty": 0, "no_qty": 0, "yes_avg": 0, "no_avg": 0})
    yes_inv = inv.get("yes_qty", 0)
    no_inv = inv.get("no_qty", 0)
    net_inventory = yes_inv - no_inv  # positive = long YES

    mid = mdata["mid"]
    spread = mdata["spread"]

    # Base quotes: inside the spread by EDGE_BUFFER
    half_spread = spread / 2
    our_half = max(1, half_spread - EDGE_BUFFER_CENTS)

    yes_bid = int(mid - our_half)
    yes_ask = int(mid + our_half)

    # Inventory skew: if long YES, lower bid (buy less) and lower ask (sell more eagerly)
    skew = int(net_inventory * INVENTORY_SKEW_CENTS)
    yes_bid -= skew
    yes_ask -= skew

    # Bounds
    yes_bid = max(1, min(98, yes_bid))
    yes_ask = max(yes_bid + MIN_PROFIT_CENTS, min(99, yes_ask))

    # Don't quote if our spread is too thin to be profitable
    our_spread = yes_ask - yes_bid
    if our_spread < MIN_PROFIT_CENTS:
        return None

    # Don't exceed inventory limits
    can_buy_yes = yes_inv < MAX_INVENTORY_PER_SIDE and (yes_inv + no_inv) < MAX_TOTAL_INVENTORY
    can_sell_yes = yes_inv > 0 or no_inv < MAX_INVENTORY_PER_SIDE

    return {
        "yes_bid": yes_bid if can_buy_yes else None,
        "yes_ask": yes_ask if can_sell_yes else None,
        "our_spread": our_spread,
        "skew": skew,
        "net_inventory": net_inventory,
    }


def execute_mm_cycle(mdata, api_func, cash_cents):
    """
    Execute one market-making cycle for a single market.

    Returns dict with actions taken.
    """
    ticker = mdata["ticker"]
    inventory = load_inventory()
    inv = inventory.get(ticker, {"yes_qty": 0, "no_qty": 0, "yes_avg": 0, "no_avg": 0, "ts": time.time(), "pnl_cents": 0})

    actions = []

    # --- Pull liquidity if near expiry ---
    if mdata["minutes_left"] < PULL_LIQUIDITY_MINS:
        if inv["yes_qty"] > 0 or inv["no_qty"] > 0:
            actions.append(f"PULL: {mdata['minutes_left']:.1f}m left — flattening inventory")
            _flatten_inventory(ticker, inv, mdata, api_func)
            inv["yes_qty"] = 0
            inv["no_qty"] = 0
            inventory[ticker] = inv
            save_inventory(inventory)
        return {"ticker": ticker, "actions": actions, "status": "pulled"}

    # --- Check if we should take profit ---
    if inv.get("pnl_cents", 0) >= FLATTEN_PROFIT_CENTS * max(inv["yes_qty"] + inv["no_qty"], 1):
        actions.append(f"PROFIT: pnl={inv['pnl_cents']}c — flattening")
        _flatten_inventory(ticker, inv, mdata, api_func)
        inv["yes_qty"] = 0
        inv["no_qty"] = 0
        inv["pnl_cents"] = 0
        inventory[ticker] = inv
        save_inventory(inventory)
        return {"ticker": ticker, "actions": actions, "status": "profit_taken"}

    # --- Calculate and post quotes ---
    # Cash limit
    max_mm_cash = int(cash_cents * MAX_CAPITAL_PCT)
    current_exposure = (inv["yes_qty"] * inv.get("yes_avg", 50) +
                       inv["no_qty"] * inv.get("no_avg", 50))
    remaining_budget = max_mm_cash - current_exposure

    quotes = calculate_quotes(mdata, inventory)
    if not quotes:
        return {"ticker": ticker, "actions": ["SKIP: spread too thin"], "status": "skip"}

    from kalshi_trade import place_order, cancel_order

    # Post YES buy (bid)
    if quotes["yes_bid"] and remaining_budget > quotes["yes_bid"] * QUOTE_SIZE:
        qty = min(QUOTE_SIZE, remaining_budget // max(quotes["yes_bid"], 1))
        if qty >= 1:
            order = place_order("prod", ticker, "buy", "yes", qty, quotes["yes_bid"])
            if order:
                oid = order.get("order_id", "")
                actions.append(f"BID: buy {qty}x YES@{quotes['yes_bid']}c (id={oid[:8]})")
                # Don't wait for fill — we'll check on next cycle
                # Track the pending order
                pending = inv.get("pending_orders", [])
                pending.append({"oid": oid, "side": "yes", "action": "buy",
                               "price": quotes["yes_bid"], "qty": qty, "ts": time.time()})
                inv["pending_orders"] = pending[-10:]

    # Post NO buy (equivalent to selling YES at the ask)
    no_bid_price = 100 - quotes["yes_ask"] if quotes["yes_ask"] else None
    if no_bid_price and no_bid_price > 0 and remaining_budget > no_bid_price * QUOTE_SIZE:
        qty = min(QUOTE_SIZE, remaining_budget // max(no_bid_price, 1))
        if qty >= 1:
            order = place_order("prod", ticker, "buy", "no", qty, no_bid_price)
            if order:
                oid = order.get("order_id", "")
                actions.append(f"ASK: buy {qty}x NO@{no_bid_price}c (= sell YES@{quotes['yes_ask']}c) (id={oid[:8]})")
                pending = inv.get("pending_orders", [])
                pending.append({"oid": oid, "side": "no", "action": "buy",
                               "price": no_bid_price, "qty": qty, "ts": time.time()})
                inv["pending_orders"] = pending[-10:]

    inv["ts"] = time.time()
    inventory[ticker] = inv
    save_inventory(inventory)

    return {"ticker": ticker, "actions": actions, "status": "quoted",
            "spread": quotes["our_spread"], "skew": quotes["skew"]}


def check_fills_and_update(api_func):
    """
    Check pending orders for fills and update inventory accordingly.
    This is the "settlement" loop — reconcile what actually filled.
    """
    inventory = load_inventory()
    updated = False

    for ticker, inv in inventory.items():
        pending = inv.get("pending_orders", [])
        new_pending = []

        for order in pending:
            oid = order.get("oid", "")
            if not oid:
                continue

            # Skip if too old (>5 min)
            if time.time() - order.get("ts", 0) > 300:
                try:
                    from kalshi_trade import cancel_order
                    cancel_order("prod", oid)
                except:
                    pass
                continue

            try:
                qr = api_func("prod", "GET", f"/portfolio/orders/{oid}")
                if qr.status_code != 200:
                    new_pending.append(order)
                    continue

                odata = qr.json().get("order", qr.json())
                filled = int(float(odata.get("fill_count_fp", "0")))
                status = odata.get("status", "")

                if filled > 0:
                    side = order["side"]
                    price = order["price"]

                    if side == "yes":
                        inv["yes_qty"] = inv.get("yes_qty", 0) + filled
                        old_avg = inv.get("yes_avg", 0)
                        old_qty = inv.get("yes_qty", 0) - filled
                        inv["yes_avg"] = int((old_avg * old_qty + price * filled) / max(inv["yes_qty"], 1))
                    else:
                        inv["no_qty"] = inv.get("no_qty", 0) + filled
                        old_avg = inv.get("no_avg", 0)
                        old_qty = inv.get("no_qty", 0) - filled
                        inv["no_avg"] = int((old_avg * old_qty + price * filled) / max(inv["no_qty"], 1))

                    _log(f"FILL {ticker}: {filled}x {side}@{price}c (oid={oid[:8]})")
                    updated = True

                    # Check if we have both sides filled = profit captured
                    if inv.get("yes_qty", 0) > 0 and inv.get("no_qty", 0) > 0:
                        pairs = min(inv["yes_qty"], inv["no_qty"])
                        pnl = pairs * (100 - inv["yes_avg"] - inv["no_avg"])
                        inv["pnl_cents"] = inv.get("pnl_cents", 0) + pnl
                        inv["yes_qty"] -= pairs
                        inv["no_qty"] -= pairs
                        _log(f"PAIRED {ticker}: {pairs} pairs, pnl={pnl}c (total={inv['pnl_cents']}c)")

                if status in ("canceled", "cancelled", "expired"):
                    continue
                elif status == "resting":
                    new_pending.append(order)
                # else: completed, don't re-add

            except Exception as e:
                new_pending.append(order)

        inv["pending_orders"] = new_pending
        inventory[ticker] = inv

    if updated:
        save_inventory(inventory)

    return inventory


def _flatten_inventory(ticker, inv, mdata, api_func):
    """Sell all held inventory at market price to flatten."""
    from kalshi_trade import place_order

    # Cancel all pending orders first
    for order in inv.get("pending_orders", []):
        try:
            from kalshi_trade import cancel_order
            cancel_order("prod", order.get("oid", ""))
        except:
            pass

    if inv.get("yes_qty", 0) > 0:
        sell_price = max(1, mdata["best_yes_bid"] - 1)
        place_order("prod", ticker, "sell", "yes", inv["yes_qty"], sell_price)
        _log(f"FLATTEN {ticker}: sell {inv['yes_qty']}x YES@{sell_price}c")

    if inv.get("no_qty", 0) > 0:
        sell_price = max(1, mdata["best_no_bid"] - 1)
        place_order("prod", ticker, "sell", "no", inv["no_qty"], sell_price)
        _log(f"FLATTEN {ticker}: sell {inv['no_qty']}x NO@{sell_price}c")


def cancel_stale_orders(api_func):
    """Cancel any resting orders that are too old."""
    inventory = load_inventory()
    for ticker, inv in inventory.items():
        for order in inv.get("pending_orders", []):
            if time.time() - order.get("ts", 0) > 120:
                try:
                    from kalshi_trade import cancel_order
                    cancel_order("prod", order.get("oid", ""))
                except:
                    pass


def get_mm_summary():
    """Get a human-readable summary of MM activity."""
    inventory = load_inventory()
    if not inventory:
        return "MM: No active positions"

    lines = []
    total_pnl = 0
    for ticker, inv in inventory.items():
        yq = inv.get("yes_qty", 0)
        nq = inv.get("no_qty", 0)
        pnl = inv.get("pnl_cents", 0)
        total_pnl += pnl
        pending = len(inv.get("pending_orders", []))
        if yq > 0 or nq > 0 or pending > 0 or pnl != 0:
            lines.append(f"  {ticker}: Y={yq} N={nq} pending={pending} pnl={pnl:+d}c")

    if not lines:
        return "MM: Idle"

    return f"MM: {len(lines)} markets, pnl={total_pnl:+d}c\n" + "\n".join(lines)


def run_mm_cycle(api_func):
    """
    Full MM cycle: check fills → evaluate markets → post quotes → manage inventory.
    Called by btc15m_scanner every minute.
    """
    results = []

    # 1. Check pending order fills
    inventory = check_fills_and_update(api_func)

    # 2. Get cash
    cash_cents = 40000
    try:
        bal_r = api_func("prod", "GET", "/portfolio/balance")
        if bal_r.status_code == 200:
            cash_cents = bal_r.json().get("balance", 0)
    except:
        pass

    # 3. Get open BTC 15M markets
    r = api_func("prod", "GET", "/markets?series_ticker=KXBTC15M&status=open&limit=10")
    if r.status_code != 200:
        return results

    markets = r.json().get("markets", [])

    for m in markets:
        ticker = m.get("ticker", "")
        mdata = get_market_data(ticker, api_func)
        if not mdata:
            continue

        ok, reason = should_mm(mdata)
        if not ok:
            # If we have inventory in a market that's no longer suitable, flatten
            if ticker in inventory and (inventory[ticker].get("yes_qty", 0) > 0 or
                                        inventory[ticker].get("no_qty", 0) > 0):
                if mdata["minutes_left"] < PULL_LIQUIDITY_MINS:
                    _flatten_inventory(ticker, inventory[ticker], mdata, api_func)
                    _log(f"FLATTEN_EXPIRY {ticker}: {reason}")
            continue

        result = execute_mm_cycle(mdata, api_func, cash_cents)
        results.append(result)
        _log(f"CYCLE {ticker}: {result['status']} actions={result['actions']}")

    # 4. Cancel stale orders
    cancel_stale_orders(api_func)

    return results
