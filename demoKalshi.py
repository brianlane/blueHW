import json
import urllib3
import uuid
import requests
import time
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== CONFIG ==================
DEMO = True
API_KEY_ID = "a7b482da-e06d-4501-83a0-b3bd4dafd328" 
PRIVATE_KEY_PATH = "kalshi_key_demo.pem"
# ===========================================

BASE_URL = "https://demo-api.kalshi.co" if DEMO else "https://api.kalshi.co"

with open(PRIVATE_KEY_PATH, "rb") as key_file:
    private_key = serialization.load_pem_private_key(key_file.read(), password=None)

def create_signature(method, path, timestamp):
    msg = timestamp + method + path
    
    signature = private_key.sign(
        msg.encode('utf-8'),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')

def kalshi_request(method, path, body=None):
    timestamp = str(int(time.time() * 1000))
    full_path = "/trade-api/v2" + path
    sig = create_signature(method, full_path, timestamp)
    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }
    
    url = BASE_URL + full_path
    if method == "POST":
        return requests.post(url, headers=headers, json=body, verify=False)
    elif method == "DELETE":
        return requests.delete(url, headers=headers, verify=False)
    return requests.get(url, headers=headers, verify=False)

# --- EXECUTION ---
print("--- Starting Kalshi Demo ---")

try:
    # 0. Auth Check
    print("0. Verifying Authentication...")
    resp = kalshi_request("GET", "/portfolio/balance")
    if resp.status_code != 200:
        print(f"Auth Failed: {resp.text}")
        exit()
    print(f"Auth Verified. Balance: ${resp.json().get('balance', 0) / 100:.2f}")

    # 1. Search Markets
    print("\n1. Searching for NYC Weather markets...")
    m_resp = kalshi_request("GET", "/markets?series_ticker=KXHIGHNY&status=open&limit=1")
    market_data = m_resp.json().get('markets', [])
    ticker = market_data[0]['ticker'] if market_data else "KXHIGHNY-26MAR14-T56"
    print(f"Found Ticker: {ticker}")

    # 2. Fetch Orderbook
    print(f"\n2. Fetching orderbook for {ticker}...")
    ob_resp = kalshi_request("GET", f"/markets/{ticker}/orderbook")
    print(f"Orderbook fetched (status {ob_resp.status_code})")

    # 3. Place Buy Order
    print(f"\n3. Placing test order for 1 unit...")
    order_data = {
        "ticker": ticker,
        "action": "buy",
        "side": "yes",
        "count": 1,
        "type": "limit",
        "yes_price": 1, # 1 cent
        "client_order_id": str(uuid.uuid4())
    }
    
    post_resp = kalshi_request("POST", "/portfolio/orders", body=order_payload if 'order_payload' in locals() else order_data)
    
    if post_resp.status_code in [200, 201]:
        order = post_resp.json().get('order', {})
        print(f"Order placed successfully! ID: {order.get('order_id')}")
        
        print(f"Canceling order {order.get('order_id')}...")
        kalshi_request("DELETE", f"/portfolio/orders/{order.get('order_id')}")
        print("Cleanup complete.")
    else:
        print(f"Error: {post_resp.status_code} - {post_resp.text}")

except Exception as e:
    print(f"Unexpected Error: {e}")

print("\n--- Script Finished ---")
