#!/usr/bin/env python3
"""
Query Hyperliquid spot order book for HYPE/USDC and calculate 
how much budget (USDC) is needed to push the price DOWN by $0.001
"""
import json
import urllib.request

def query_hyperliquid(payload):
    url = "https://api.hyperliquid.xyz/info"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}

# Current HYPE price from allMids
mids = query_hyperliquid({"type": "allMids"})
hype_price = float(mids.get('HYPE', 0))
print(f"HYPE current mid price: ${hype_price}")

# Check what's available for @107 (HYPE/USDC - token index 150,0)
# Try candleSnapshot as proxy for depth
print("\n=== candleSnapshot for HYPE (15m) ===")
try:
    cs = query_hyperliquid({
        "type": "candleSnapshot",
        "coin": "HYPE",
        "interval": "15m"
    })
    print(json.dumps(cs, indent=2)[:3000])
except Exception as e:
    print(f"Error: {e}")

# Try chartCandles
print("\n=== chartCandles for HYPE ===")
try:
    cc = query_hyperliquid({
        "type": "chartCandles",
        "coin": "HYPE",
        "interval": "1h"
    })
    print(json.dumps(cc, indent=2)[:3000])
except Exception as e:
    print(f"Error: {e}")

# The token ID for HYPE is 150 - check if there's a swap pool
# Let's check if the issue is that HYPE spot isn't listed in the exchange
# but exists in allMids (oracle price?)
print("\n=== Checking if HYPE has a dedicated order book or is oracle-priced ===")
# Try "clearingConfig" or "funding" 
fc = query_hyperliquid({"type": "fundingChartData", "coin": "HYPE"})
print(f"fundingChartData: {fc}")

# Try to understand the spotMeta universe better
# Let's see what tokens 150 maps to
universe = query_hyperliquid({"type": "spotMeta"}).get('universe', [])
# Find all with token[0] == 150
hype_pairs = [u for u in universe if u.get('tokens', [0, 0])[0] == 150]
print(f"\nPairs with token 150 (HYPE): {hype_pairs}")

# Also find pairs where price is ~44.6 in allMids
print(f"\nPairs with price ~44.6 in allMids:")
for k, v in mids.items():
    try:
        if 40 < float(v) < 50:
            print(f"  {k}: {v}")
    except:
        pass

