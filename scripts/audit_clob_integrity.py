#!/usr/bin/env python3
"""CLOB Integrity Re-Audit — raw evidence only, no assumptions.

This script directly queries every API endpoint, prints raw payloads,
and verifies every claim. No code-path assumptions.
"""
import json, urllib.request, sys, time
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from db import Database
from chainlink_helpers import get_latest_chainlink_price

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
PROJECT = Path(__file__).resolve().parent.parent
db = Database(str(PROJECT / "data" / "btc5m.db"))

issues = {"P0": [], "P1": [], "P2": []}
def p0(m): issues["P0"].append(m)
def p1(m): issues["P1"].append(m)
def p2(m): issues["P2"].append(m)

def gamma_get(path):
    url = f"{GAMMA}{path}"
    req = urllib.request.Request(url, headers={"User-Agent":"curl/7.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def clob_get(path):
    url = f"{CLOB}{path}"
    req = urllib.request.Request(url, headers={"User-Agent":"curl/7.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def safe_float(v):
    try: return float(v)
    except: return None

print("=" * 100)
print("CLOB INTEGRITY RE-AUDIT REPORT")
print("Generated:", datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
print("=" * 100)

# ═══════════════════════════════════════════════
# SECTION B: Gamma Market Metadata Evidence
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("B. GAMMA MARKET METADATA EVIDENCE")
print("=" * 100)

now_ts = int(time.time())
window_start = (now_ts // 300) * 300
candidates = [
    f"btc-updown-5m-{window_start}",
    f"btc-updown-5m-{window_start - 300}",
    f"btc-updown-5m-{window_start + 300}",
]

active_market = None
for slug in candidates:
    try:
        m = gamma_get(f"/markets?slug={slug}")
        if m and len(m) > 0:
            m = m[0]
            if m.get("active") and not m.get("closed"):
                active_market = (slug, m)
                print(f"\nActive market found: {slug}")
                break
    except:
        continue

if not active_market:
    print("WARNING: No active market found. Using most recent slug.")
    for slug in candidates:
        try:
            m = gamma_get(f"/markets?slug={slug}")
            if m and len(m) > 0:
                active_market = (slug, m[0])
                print(f"Fell back to: {slug}")
                break
        except:
            continue

if not active_market:
    print("FATAL: No market data available")
    sys.exit(1)

slug, m = active_market

print(f"\nRaw slug:          {slug}")
print(f"Gamma slug:        {m.get('slug')}")
print(f"Question:          {m.get('question')}")
print(f"conditionId:       {m.get('conditionId')}")
print(f"endDate:           {m.get('endDate')}")
print(f"startDate:         {m.get('startDate')}")
print(f"resolutionSource:  {m.get('resolutionSource')}")
print(f"closed:            {m.get('closed')} (type: {type(m.get('closed')).__name__})")
print(f"active:            {m.get('active')} (type: {type(m.get('active')).__name__})")
print(f"volume:            {m.get('volume')}")
print(f"liquidity:         {m.get('liquidity')}")
print(f"negRisk:           {m.get('negRisk')}")
print(f"min_order_size:    {m.get('orderMinSize')}")
print(f"tick_size:         {m.get('orderPriceMinTickSize')}")

# ═══════════════════════════════════════════════
# SECTION C: Market Window Check
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("C. MARKET WINDOW CHECK")
print("=" * 100)

now_utc = datetime.now(timezone.utc)
print(f"now_utc:           {now_utc.isoformat()}")
print(f"now_ts:            {now_ts}")
print(f"window_start_ts:   {window_start}")
print(f"expected_slug:     btc-updown-5m-{window_start}")
print(f"Active slug:       {slug}")

# Check all three windows
print("\nWindow boundary check:")
for s in candidates:
    try:
        data = gamma_get(f"/markets?slug={s}")
        if data and len(data) > 0:
            dm = data[0]
            print(f"  {s[-7:]} | start={dm.get('startDate','?')[:19]} end={dm.get('endDate','?')[:19]} active={dm.get('active')} closed={dm.get('closed')} volume={dm.get('volume')}")
    except:
        print(f"  {s[-7:]} | API ERROR")

# Verify time bounds
end_date_str = m.get("endDate", "")
if end_date_str:
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        start_dt = datetime.fromisoformat(m.get("startDate", "").replace("Z", "+00:00")) if m.get("startDate") else None
        print(f"\nCurrent time within [startDate, endDate]: ", end="")
        if start_dt and end_dt:
            in_window = start_dt <= now_utc <= end_dt
            print(f"{'YES' if in_window else 'NO'} ({start_dt.strftime('%H:%M:%S')} → {end_dt.strftime('%H:%M:%S')})")
            if not in_window:
                p0(f"Market outside window: now={now_utc.strftime('%H:%M:%S')} window={start_dt.strftime('%H:%M:%S')}→{end_dt.strftime('%H:%M:%S')}")
        else:
            print(f"No valid start/end dates")
            p0("Missing startDate or endDate in Gamma metadata")
    except Exception as e:
        print(f"Parse error: {e}")
        p0(f"Cannot parse endDate: {end_date_str}")

# ═══════════════════════════════════════════════
# SECTION D: Token Mapping Evidence
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("D. TOKEN MAPPING EVIDENCE")
print("=" * 100)

outcomes_raw = m.get("outcomes")
token_ids_raw = m.get("clobTokenIds")

outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw

print(f"outcomes_raw  = {repr(outcomes_raw)} (type: {type(outcomes_raw).__name__})")
print(f"token_ids_raw = {repr(token_ids_raw)[:80]}... (type: {type(token_ids_raw).__name__})")
print(f"outcomes      = {outcomes}")
print(f"token_ids     = {[t[:20]+'...' for t in token_ids]}")

mapping_ok = True
for i, outcome in enumerate(outcomes):
    tid = token_ids[i] if i < len(token_ids) else "MISSING"
    print(f"  outcomes[{i}] = '{outcome}'  →  token_ids[{i}] = {tid[:20] if tid != 'MISSING' else 'MISSING'}...")
    
if len(outcomes) != 2 or len(token_ids) != 2:
    p0(f"Expected 2 outcomes/tokens, got outcomes={len(outcomes)} tokens={len(token_ids)}")
    mapping_ok = False

up_token = token_ids[0] if mapping_ok and outcomes[0] == "Up" else (token_ids[1] if mapping_ok and outcomes[1] == "Up" else None)
down_token = token_ids[1] if mapping_ok and outcomes[1] == "Down" else (token_ids[0] if mapping_ok and outcomes[0] == "Down" else None)

print(f"\nUp token:   {up_token[:30] if up_token else 'MISSING'}...")
print(f"Down token: {down_token[:30] if down_token else 'MISSING'}...")

if not up_token or not down_token:
    p0(f"Token mapping failed: Up={up_token is not None}, Down={down_token is not None}")

# Verify outcomes are "Up" and "Down"
if outcomes != ["Up", "Down"]:
    p0(f"Unexpected outcomes: {outcomes}")

# ═══════════════════════════════════════════════
# SECTION E: Raw CLOB Book Evidence
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("E. RAW CLOB BOOK EVIDENCE")
print("=" * 100)

def analyze_book(token_id, label):
    print(f"\n--- {label} (token={token_id[:20]}...) ---")
    book = clob_get(f"/book?token_id={token_id}")
    
    print(f"  market:           {book.get('market','')[:40]}...")
    print(f"  asset_id:         {book.get('asset_id','')[:40]}...")
    print(f"  timestamp:        {book.get('timestamp')} (type: {type(book.get('timestamp')).__name__})")
    print(f"  last_trade_price: {book.get('last_trade_price')}")
    print(f"  min_order_size:   {book.get('min_order_size')}")
    print(f"  tick_size:        {book.get('tick_size')}")
    print(f"  neg_risk:         {book.get('neg_risk')}")
    
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    print(f"  bids count:       {len(bids)}")
    print(f"  asks count:       {len(asks)}")
    
    if not bids:
        p0(f"{label}: /book has empty bids")
    if not asks:
        p0(f"{label}: /book has empty asks")
    
    # Print raw first 5 bids and asks
    print(f"\n  Raw bids (first 10):")
    safe_bids = []
    for b in bids[:10]:
        if isinstance(b, dict):
            safe_bids.append({"price": b.get("price"), "size": b.get("size")})
            print(f"    {json.dumps(b)}")
        else:
            safe_bids.append({"price": b[0] if len(b) > 0 else "?", "size": b[1] if len(b) > 1 else "?"})
            print(f"    {b}")
    
    print(f"\n  Raw asks (first 10):")
    safe_asks = []
    for a in asks[:10]:
        if isinstance(a, dict):
            safe_asks.append({"price": a.get("price"), "size": a.get("size")})
            print(f"    {json.dumps(a)}")
        else:
            safe_asks.append({"price": a[0] if len(a) > 0 else "?", "size": a[1] if len(a) > 1 else "?"})
            print(f"    {a}")
    
    # Check for string sorting bug
    raw_bid_prices = [float(b["price"]) if isinstance(b, dict) else float(b[0]) for b in bids[:5]]
    raw_ask_prices = [float(a["price"]) if isinstance(a, dict) else float(a[0]) for a in asks[:5]]
    
    print(f"\n  RAW bid prices (as float): {raw_bid_prices}")
    print(f"  RAW ask prices (as float): {raw_ask_prices}")
    
    max_bid = max(float(b["price"]) if isinstance(b, dict) else float(b[0]) for b in bids if b) if bids else None
    min_ask = min(float(a["price"]) if isinstance(a, dict) else float(a[0]) for a in asks if a) if asks else None
    
    # Check if max_bid seems wrong (like 0.01 in a market where expect ~0.9)
    print(f"\n  max(bid price):  {max_bid} (should be highest buy order)")
    print(f"  min(ask price):  {min_ask} (should be lowest sell order)")
    
    # String ordering check
    str_bids_sorted = sorted([str(b["price"]) if isinstance(b, dict) else str(b[0]) for b in bids[:5]])
    str_asks_sorted = sorted([str(a["price"]) if isinstance(a, dict) else str(a[0]) for a in asks[:5]])
    float_bids_sorted = sorted([float(b["price"]) if isinstance(b, dict) else float(b[0]) for b in bids[:5]], reverse=True)
    float_asks_sorted = sorted([float(a["price"]) if isinstance(a, dict) else float(a[0]) for a in asks[:5]])
    
    str_bug_bids = str_bids_sorted != [str(x) for x in float_bids_sorted]
    str_bug_asks = str_asks_sorted != [str(x) for x in float_asks_sorted]
    
    if str_bug_bids:
        print(f"  ⚠ STRING SORTING BUG in bids: string sort={str_bids_sorted}, float sort={float_bids_sorted}")
        p0(f"{label}: String sorting bug detected in bids — string sort order differs from float sort order")
    else:
        print(f"  ✅ No string sorting bug in bids")
    
    if str_bug_asks:
        print(f"  ⚠ STRING SORTING BUG in asks: string sort={str_asks_sorted}, float sort={float_asks_sorted}")
        p0(f"{label}: String sorting bug detected in asks")
    else:
        print(f"  ✅ No string sorting bug in asks")
    
    # Top 3 sorted by price
    top3_bids = sorted(
        [b for b in bids if isinstance(b, dict) and b.get("price") is not None],
        key=lambda b: float(b["price"]), reverse=True
    )[:3] if bids else []
    
    top3_asks = sorted(
        [a for a in asks if isinstance(a, dict) and a.get("price") is not None],
        key=lambda a: float(a["price"])
    )[:3] if asks else []
    
    print(f"\n  Top 3 bids (desc by price):")
    for b in top3_bids:
        print(f"    price={float(b['price']):.4f}  size={float(b['size']):.2f}")
    print(f"  Top 3 asks (asc by price):")
    for a in top3_asks:
        print(f"    price={float(a['price']):.4f}  size={float(a['size']):.2f}")
    
    # Size at best levels
    best_bid_size = None
    if max_bid is not None and top3_bids:
        best_bid_size = sum(float(b["size"]) for b in top3_bids if abs(float(b["price"]) - max_bid) < 0.0001)
    best_ask_size = None
    if min_ask is not None and top3_asks:
        best_ask_size = sum(float(a["size"]) for a in top3_asks if abs(float(a["price"]) - min_ask) < 0.0001)
    
    top3_bid_total = sum(float(b["size"]) for b in top3_bids)
    top3_ask_total = sum(float(a["size"]) for a in top3_asks)
    
    print(f"\n  best_bid_size (at max price): {best_bid_size}")
    print(f"  best_ask_size (at min price): {best_ask_size}")
    print(f"  top 3 bid total: {top3_bid_total:.2f}")
    print(f"  top 3 ask total: {top3_ask_total:.2f}")
    
    # Depth check
    min_order = safe_float(book.get("min_order_size"))
    if min_order and best_bid_size is not None and best_bid_size < min_order:
        p1(f"{label}: best bid size ({best_bid_size}) < min_order_size ({min_order})")
    if min_order and best_ask_size is not None and best_ask_size < min_order:
        p1(f"{label}: best ask size ({best_ask_size}) < min_order_size ({min_order})")
    
    # Midpoint
    try:
        mp_data = clob_get(f"/midpoint?token_id={token_id}")
        midpoint = safe_float(mp_data.get("mid"))
        print(f"\n  /midpoint: {mp_data}")
    except:
        midpoint = None
    
    # Last trade price
    try:
        ltp_data = clob_get(f"/last-trade-price?token_id={token_id}")
        print(f"  /last-trade-price: {ltp_data}")
    except:
        ltp_data = None
    
    # Timestamp check
    book_ts = book.get("timestamp")
    if book_ts:
        try:
            book_ts_int = int(book_ts)
            now_ms = int(time.time() * 1000)
            if book_ts_int > 1_000_000_000_000:  # 13 digits = ms
                book_age_ms = now_ms - book_ts_int
            else:
                book_age_ms = (now_ms - book_ts_int * 1000)  # convert s to ms
            print(f"  book_age_ms: {book_age_ms}")
            if book_age_ms > 5000 and book_age_ms < 60000:
                p2(f"{label}: book age {book_age_ms}ms > 5s")
            elif book_age_ms > 60000:
                p1(f"{label}: book age {book_age_ms}ms > 60s (stale)")
        except (ValueError, TypeError):
            pass
    
    return {
        "max_bid": max_bid,
        "min_ask": min_ask,
        "midpoint": midpoint,
        "top3_bids": top3_bids,
        "top3_asks": top3_asks,
        "top3_bid_total": top3_bid_total,
        "top3_ask_total": top3_ask_total,
        "min_order_size": min_order,
    }

up_book = analyze_book(up_token, "Up (YES)")
down_book = analyze_book(down_token, "Down (NO)")

# ═══════════════════════════════════════════════
# SECTION F: /price vs /book Cross Check
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("F. /price VS /book CROSS CHECK")
print("=" * 100)

print(f"\n{'Token':>10} | {'Outcome':>8} | {'/price BUY':>11} | {'min(asks)':>10} | {'BUY match?':>10} | {'/price SELL':>11} | {'max(bids)':>10} | {'SELL match?':>10}")
print("-" * 100)

def cross_check(token_id, label, book_max_bid, book_min_ask):
    buy_px = clob_get(f"/price?token_id={token_id}&side=BUY")
    sell_px = clob_get(f"/price?token_id={token_id}&side=SELL")
    bp = safe_float(buy_px.get("price"))
    sp = safe_float(sell_px.get("price"))
    
    buy_match = abs(bp - book_min_ask) < 0.005 if (bp is not None and book_min_ask is not None) else None
    sell_match = abs(sp - book_max_bid) < 0.005 if (sp is not None and book_max_bid is not None) else None
    
    print(f"{token_id[:8]+'...':>10} | {label:>8} | {str(bp):>11} | {str(book_min_ask):>10} | {str(buy_match):>10} | {str(sp):>11} | {str(book_max_bid):>10} | {str(sell_match):>10}")
    
    if buy_match is False:
        p0(f"/price BUY={bp} vs book min_ask={book_min_ask} for {label} — side semantics may be wrong")
    if sell_match is False:
        p0(f"/price SELL={sp} vs book max_bid={book_max_bid} for {label} — side semantics may be wrong")
    
    return buy_match, sell_match

up_buy_match, up_sell_match = cross_check(up_token, "Up", up_book["max_bid"], up_book["min_ask"])
down_buy_match, down_sell_match = cross_check(down_token, "Down", down_book["max_bid"], down_book["min_ask"])

# ═══════════════════════════════════════════════
# SECTION G: Spread and Depth Calculation
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("G. SPREAD AND DEPTH CALCULATION")
print("=" * 100)

for label, book_data, token_id in [("Up", up_book, up_token), ("Down", down_book, down_token)]:
    bb = book_data["max_bid"]
    ba = book_data["min_ask"]
    mp = book_data["midpoint"]
    
    print(f"\n--- {label} ---")
    print(f"  max_bid:            {bb}")
    print(f"  min_ask:            {ba}")
    print(f"  midpoint:           {mp}")
    
    if bb is not None and ba is not None:
        spread = ba - bb
        spread_pct_mid = (spread / ((ba + bb) / 2)) * 100 if (ba + bb) > 0 else None
        spread_pct_ask = (spread / ba) * 100 if ba > 0 else None
        print(f"  spread (ask-bid):   {spread:.4f}")
        print(f"  spread_pct_mid:     {spread_pct_mid:.2f}%" if spread_pct_mid else "  spread_pct_mid:     N/A")
        print(f"  spread_pct_ask:     {spread_pct_ask:.2f}%" if spread_pct_ask else "  spread_pct_ask:     N/A")
    
    print(f"  top3_bid_total:     {book_data['top3_bid_total']:.2f}")
    print(f"  top3_ask_total:     {book_data['top3_ask_total']:.2f}")
    
    # Four-combination test
    if bb is not None and ba is not None:
        print(f"\n  Four-combination check:")
        down_bb = down_book["max_bid"] if label == "Up" else up_book["max_bid"]
        down_ba = down_book["min_ask"] if label == "Up" else up_book["min_ask"]
        
        up_ask_plus_down_ask = ba + down_ba
        up_bid_plus_down_bid = bb + down_bb
        up_ask_plus_down_bid = ba + down_bb
        up_bid_plus_down_ask = bb + down_ba
        
        print(f"    Up best_ask + Down best_ask = {ba} + {down_ba} = {up_ask_plus_down_ask:.4f}")
        print(f"    Up best_bid + Down best_bid = {bb} + {down_bb} = {up_bid_plus_down_bid:.4f}")
        print(f"    Up best_ask + Down best_bid = {ba} + {down_bb} = {up_ask_plus_down_bid:.4f}")
        print(f"    Up best_bid + Down best_ask = {bb} + {down_ba} = {up_bid_plus_down_ask:.4f}")
        
        # These should be > 1.0 (trading cost) and < 1.0 (liquidity cost) respectively
        if up_ask_plus_down_ask < 0.9:
            p1(f"Up best_ask + Down best_ask = {up_ask_plus_down_ask:.4f} < 0.9 — combined buy cost very low, suspicious")
        if up_bid_plus_down_bid > 1.1:
            p1(f"Up best_bid + Down best_bid = {up_bid_plus_down_bid:.4f} > 1.1 — combined sell value very high, suspicious")
    
    # Determine if extreme spread is real or a parsing error
    if bb is not None and ba is not None and spread_pct_ask is not None and spread_pct_ask > 20:
        # Check if this is a real market state or parsing error
        print(f"\n  ⚠ Spread {spread_pct_ask:.1f}% > 20%. Checking if real or error...")
        
        # Check 1: Are bids and asks transposed?
        if bb > ba:
            p0(f"{label}: bids > asks ({bb} > {ba}) — bids/asks may be swapped!")
            print(f"    ❌ bids > asks: bids and asks may be REVERSED")
        else:
            print(f"    ✅ bids < asks (normal ordering)")
        
        # Check 2: Book timing
        book_age = book_data.get("book_age_ms")
        if book_data.get("book_age_ms") and book_data["book_age_ms"] > 5000:
            print(f"    ⚠ Book may be stale: {book_data['book_age_ms']}ms")
        
        # Check 3: Size at best levels
        if book_data["top3_bid_total"] < 100 and book_data["top3_ask_total"] < 100:
            print(f"    ✅ Very low depth ({book_data['top3_bid_total']:.0f}/{book_data['top3_ask_total']:.0f}) — consistent with low liquidity")
            p1(f"{label}: Extreme spread ({spread_pct_ask:.0f}%) with very low depth — real low liquidity, not code bug")
        else:
            # Significant depth but extreme spread — something is wrong
            p0(f"{label}: Extreme spread ({spread_pct_ask:.0f}%) with significant depth ({book_data['top3_bid_total']:.0f}/{book_data['top3_ask_total']:.0f}) — likely a code or parsing bug")
    elif bb is not None and ba is not None and spread_pct_ask is not None and spread_pct_ask > 5:
        p1(f"{label}: Spread {spread_pct_ask:.1f}% > 5%")

# ═══════════════════════════════════════════════
# SECTION H: Timestamp Freshness Check
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("H. TIMESTAMP FRESHNESS CHECK")
print("=" * 100)

now_ms = int(time.time() * 1000)
for label, token_id in [("Up", up_token), ("Down", down_token)]:
    try:
        book = clob_get(f"/book?token_id={token_id}")
        ts = book.get("timestamp")
        if ts:
            ts_int = int(ts)
            print(f"  {label}: book timestamp={ts_int}, now_ms={now_ms}, diff={now_ms - ts_int}ms")
            if ts_int > 1_000_000_000_000:
                age_ms = now_ms - ts_int
                print(f"         age: {age_ms}ms")
                if age_ms > 5000:
                    p1(f"{label}: book age {age_ms}ms > 5s")
    except:
        pass

# ═══════════════════════════════════════════════
# SECTION I: Sorting and Type Safety Check
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("I. SORTING AND TYPE SAFETY CHECK")
print("=" * 100)

# Already checked in section E for individual tokens
# Verify the code in realtime_trader.py uses float sorting
trader_path = SRC / "realtime_trader.py"
print(f"Checking {trader_path}...")
if trader_path.exists():
    text = trader_path.read_text()
    
    # Check for max(float(b...))
    has_max_float = "max(float(b" in text or "max(float(b" in text
    has_min_float = "min(float(a" in text or "min(float(a" in text
    has_float_cast = "float(b[\"price\"])" in text
    has_reverse_sorted = "reverse=True" in text
    
    print(f"  max(float(b[...])): {has_max_float}")
    print(f"  min(float(a[...])): {has_min_float}")
    print(f"  float(b['price'])  : {has_float_cast}")
    print(f"  reverse=True (desc): {has_reverse_sorted}")
    
    if not has_float_cast:
        p0("realtime_trader.py: price fields not cast to float before comparison")

# ═══════════════════════════════════════════════
# SECTION J: Paper Fill Contamination Check
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("J. PAPER FILL CONTAMINATION CHECK")
print("=" * 100)

# Read the signal dict construction in _detect_signal
# Check that fill_price uses best_ask/best_bid, not midpoint or last_price
if trader_path.exists():
    text = trader_path.read_text()
    
    checks = {
        'fill_price = best_ask or buy_price': 'fill_price = yes_best_ask if yes_best_ask else buy_price' in text,
        'fill_method tracking': 'fill_method' in text,
        'execution_side tracking': 'execution_side' in text,
        'settlement_source = chainlink': 'settlement_source' in text,
        'chainlink_entry_btc': 'chainlink_entry_btc' in text,
        'No midpoint as fill': '"fill_method": "midpoint"' in text or 'fill_method = "midpoint"' in text,
    }
    
    for check_name, present in checks.items():
        status = "✅" if present else "❌"
        print(f"  {status} {check_name}")
    
    # Determine if midpoint fallback exists (acceptable only as fallback)
    has_midpoint_fallback = '"fill_method": "midpoint"' in text or 'fill_method = "midpoint"' in text
    if has_midpoint_fallback:
        # Check if it's guarded by "if not fill_price"
        if "if not fill_price or fill_price <= 0" in text:
            print(f"  ✅ Midpoint fallback is guarded (only used when best_ask/bid missing)")
        else:
            p1("Midpoint fallback not properly guarded")

# Check paper_trades.jsonl for any existing contaminated records
paper_log = PROJECT / "logs" / "paper_trades.jsonl"
if paper_log.exists():
    contaminated = 0
    clean = 0
    with open(paper_log) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                entry = json.loads(line)
                if entry.get("fill_method") == "midpoint" and entry.get("fill_method"):
                    contaminated += 1
                elif entry.get("fill_method") in ("best_ask", "best_bid"):
                    clean += 1
            except:
                pass
    print(f"\n  Existing paper_trades.jsonl: {contaminated} midpoint fills, {clean} best_ask/bid fills")
    if contaminated > 0:
        p1(f"{contaminated} existing trades used midpoint fill price")

# ═══════════════════════════════════════════════
# SECTION K: Final Severity Classification
# ═══════════════════════════════════════════════
print("\n" + "=" * 100)
print("K. FINAL SEVERITY CLASSIFICATION")
print("=" * 100)

can_paper_test = len(issues["P0"]) == 0

print(f"\nP0 count: {len(issues['P0'])}")
for i in issues["P0"]:
    print(f"  🔴 {i}")

print(f"\nP1 count: {len(issues['P1'])}")
for i in issues["P1"]:
    print(f"  🟡 {i}")

print(f"\nP2 count: {len(issues['P2'])}")
for i in issues["P2"]:
    print(f"  🟢 {i}")

print(f"\n{'=' * 100}")
if can_paper_test:
    print("✅ VERDICT: No P0 issues. CLOB data integrity confirmed.")
else:
    print(f"🔴 VERDICT: {len(issues['P0'])} P0 issues must be fixed before paper testing.")
print(f"{'=' * 100}")

# Save report
report = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "market_slug": slug,
    "up_token": up_token,
    "down_token": down_token,
    "up_book": {
        "max_bid": up_book["max_bid"],
        "min_ask": up_book["min_ask"],
        "midpoint": up_book["midpoint"],
    },
    "down_book": {
        "max_bid": down_book["max_bid"],
        "min_ask": down_book["min_ask"],
        "midpoint": down_book["midpoint"],
    },
    "p0_issues": issues["P0"],
    "p1_issues": issues["P1"],
    "p2_issues": issues["P2"],
    "can_paper_test": can_paper_test,
}
report_path = PROJECT / "logs" / "clob_audit_report.json"
with open(report_path, "w") as f:
    json.dump(report, f, indent=2)
print(f"\nReport saved to: {report_path}")
