#!/usr/bin/env python3
"""Pre-load all settlements and save to a JSON cache."""
import json, sqlite3, urllib.request, time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(".")
CACHE = PROJECT / "scripts" / "settlement_cache.json"

db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))
cur = db.cursor()

start_ts = 1777766100
end_ts = int(time.time())

window_slugs = []
ws = (start_ts // 300) * 300
while ws + 300 <= end_ts:
    window_slugs.append((f"btc-updown-5m-{ws}", ws, ws + 300))
    ws += 300

print(f"Total windows: {len(window_slugs)}")

settlements = {}
for slug, ws, we in window_slugs:
    cur.execute("SELECT resolution FROM markets WHERE slug=?", (slug,))
    row = cur.fetchone()
    if row and row[0]:
        settlements[slug] = row[0]

print(f"Settlements in DB: {len(settlements)}")

missing = [(s, ws) for s, ws, we in window_slugs if s not in settlements]
print(f"Missing from DB: {len(missing)}")

for i, (slug, ws) in enumerate(missing):
    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list): data = data[0] if data else {}
        prices_raw = data.get("outcomePrices")
        outcomes_raw = data.get("outcomes")
        if prices_raw and outcomes_raw:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            prices = [float(p) for p in prices]
            if data.get("closed") and max(prices) >= 0.99:
                winner = outcomes[prices.index(max(prices))]
                settlements[slug] = winner
                try:
                    cur.execute("UPDATE markets SET resolution=? WHERE slug=?", (winner, slug))
                    db.commit()
                except: pass
    except Exception as e:
        pass
    if (i+1) % 10 == 0:
        print(f"  Progress: {i+1}/{len(missing)}")

with open(CACHE, "w") as f:
    json.dump(settlements, f)
print(f"Cached {len(settlements)} settlements")
db.close()
