#!/usr/bin/env python3
"""Direct comparison of the two conflicting results."""
import json, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(".")
CACHE = PROJECT / "scripts" / "settlement_cache.json"
with open(CACHE) as f:
    SETTLEMENTS = json.load(f)

WINDOW_LEN = 300; ENTRY_DELAY = 20

db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))
cur = db.cursor()
ws = 1777766100; all_w = []
while ws + 300 <= int(time.time()):
    slug = f"btc-updown-5m-{ws}"
    st = SETTLEMENTS.get(slug)
    s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws))
    e = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws+WINDOW_LEN))
    cur.execute("SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid", (s, e))
    btc = [(r[0], float(r[1])) for r in cur.fetchall()]
    cur.execute("SELECT m.market_id FROM markets m WHERE m.slug=?", (slug,))
    row = cur.fetchone()
    yes = []
    if row:
        cur.execute("SELECT timestamp, midpoint FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? ORDER BY rowid", (row[0], s, e))
        yes = [(r[0], float(r[1]) if r[1] else 0.5) for r in cur.fetchall()]
    if len(btc) >= 5 and len(yes) >= 3 and st: all_w.append((slug, ws, btc, yes, st))
    ws += 300
db.close()

print(f"Settlement cache entries: {len(SETTLEMENTS)}")
print(f"Tradable windows: {len(all_w)}")

# ======================================================================
# METHOD A: compare_btc5m_params.py logic (Codex's "New" params)
# Params: btc_yes=3, btc_no=10, yes_low=0.30, yes_high=0.45,
#         sl_yes=3, sl_no=3, max_entries=2, size=10
# ======================================================================
print("\n=== METHOD A: compare_btc5m_params.py logic (Codex) ===")
print("Params: btc_yes=3, btc_no=10, yes_low=0.30, yes_high=0.45, sl_yes=3, sl_no=3, max_entries=2, size=10")

def codex_sim(params, windows):
    trades = []
    for slug, window_start, btc_ticks, yes_snapshots, settlement in windows:
        open_price = btc_ticks[0][1]
        open_trades = []
        snap_idx = 0
        yes_mid = 0.5

        for timestamp, btc_price in btc_ticks:
            tick_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
            elapsed = tick_time - window_start
            if elapsed < 0 or elapsed >= WINDOW_LEN:
                continue
            btc_change = btc_price - open_price
            while snap_idx < len(yes_snapshots) and yes_snapshots[snap_idx][0] <= timestamp:
                yes_mid = yes_snapshots[snap_idx][1]
                snap_idx += 1
            if yes_mid <= 0.001 or yes_mid >= 0.999:
                continue
            if elapsed >= ENTRY_DELAY:
                can_enter = True
                if open_trades and len(open_trades) >= params.max_entries:
                    can_enter = False
                if open_trades and elapsed < open_trades[-1]["entry_elapsed"] + 20:
                    can_enter = False
                if can_enter and btc_change > params.btc_no and elapsed <= 180 and yes_mid <= params.yes_low:
                    open_trades.append({"side": "NO", "entry_yes": yes_mid, "entry_elapsed": elapsed, "entry_btc_change": btc_change, "exit_reason": None})
                elif can_enter and btc_change < -params.btc_yes and elapsed <= 270 and yes_mid >= params.yes_high:
                    open_trades.append({"side": "YES", "entry_yes": yes_mid, "entry_elapsed": elapsed, "entry_btc_change": btc_change, "exit_reason": None})
            if elapsed >= 285:
                continue
            for trade in open_trades:
                if trade["exit_reason"]:
                    continue
                if trade["side"] == "YES" and yes_mid >= params.tp_yes:
                    pnl = round((yes_mid - trade["entry_yes"]) * params.size, 2)
                    trade["pnl"] = pnl; trade["exit_reason"] = "TP"; trade["exit_yes"] = yes_mid
                    continue
                if trade["side"] == "NO" and 1.0 - yes_mid >= params.tp_no:
                    pnl = round((trade["entry_yes"] - yes_mid) * params.size, 2)
                    trade["pnl"] = pnl; trade["exit_reason"] = "TP"; trade["exit_yes"] = yes_mid
                    continue
                sl_mult = params.sl_yes if trade["side"] == "YES" else params.sl_no
                threshold = max(abs(trade["entry_btc_change"]) * sl_mult, 10.0)
                if trade["side"] == "YES" and trade["entry_btc_change"] < 0 and btc_change < -threshold:
                    pnl = round((yes_mid - trade["entry_yes"]) * params.size, 2)
                    trade["pnl"] = pnl; trade["exit_reason"] = "SL"; trade["exit_yes"] = yes_mid
                elif trade["side"] == "NO" and trade["entry_btc_change"] > 0 and btc_change > threshold:
                    pnl = round((trade["entry_yes"] - yes_mid) * params.size, 2)
                    trade["pnl"] = pnl; trade["exit_reason"] = "SL"; trade["exit_yes"] = yes_mid
        for trade in open_trades:
            if not trade["exit_reason"]:
                entry_price = trade["entry_yes"] if trade["side"] == "YES" else 1.0 - trade["entry_yes"]
                won = (settlement == "Up" and trade["side"] == "YES") or (settlement == "Down" and trade["side"] == "NO")
                pnl = round((1.0 - entry_price) * params.size, 2) if won else round(-entry_price * params.size, 2)
                trade["pnl"] = pnl; trade["exit_reason"] = "Settlement"
            trade["settlement"] = settlement
            trades.append(trade)
    return trades

from dataclasses import dataclass
@dataclass(frozen=True)
class Params:
    btc_yes: float; btc_no: float; yes_low: float; yes_high: float
    sl_yes: float; sl_no: float; max_entries: int; tp_yes: float; tp_no: float; size: float

codex_new = Params(btc_yes=3, btc_no=10, yes_low=0.30, yes_high=0.45, sl_yes=3, sl_no=3, max_entries=2, tp_yes=0.85, tp_no=0.88, size=10)
trades_a = codex_sim(codex_new, all_w)
pnl_a = sum(t["pnl"] for t in trades_a)
w_a = sum(1 for t in trades_a if t["pnl"] > 0)
l_a = sum(1 for t in trades_a if t["pnl"] < 0)
f_a = sum(1 for t in trades_a if t["pnl"] == 0)
print(f"PnL: ${pnl_a:+.2f}, WR: {w_a}/{len(trades_a)} ({w_a/len(trades_a)*100:.1f}%), Trades: {len(trades_a)}")

# ======================================================================
# METHOD B: enhanced_sweep.py / final_run.py logic
# Params: btc_yes=3, btc_no=10, yes_low=0.30, yes_high=0.45,
#         sl_yes=3, sl_no=3, max_entries=2, size=8
# ======================================================================
print("\n=== METHOD B: enhanced_sweep.py logic (size=8, max_entries=2) ===")
print("Params: btc_yes=3, btc_no=10, yes_low=0.30, yes_high=0.45, sl_yes=3, sl_no=3, max_entries=2, size=8")

def sweep_sim(btc_yes, btc_no, yl, yh, sl_yes, sl_no, max_entries, wins):
    tp_pnl = 0; tp_w = 0; tp_l = 0; tp_tp = 0; tp_sl = 0; tp_st = 0
    tp_pnl_tp = 0; tp_pnl_sl = 0; tp_pnl_st = 0
    for slug, ws, btc_t, yes_s, st in wins:
        if len(btc_t) < 5 or len(yes_s) < 3 or not st: continue
        op = btc_t[0][1]; ent = []; si = 0; ym = 0.5
        for ts, bp in btc_t:
            td = datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
            el = td - ws
            if el < 0 or el >= WINDOW_LEN: continue
            bc = bp - op
            while si < len(yes_s) and yes_s[si][0] <= ts: ym = yes_s[si][1]; si += 1
            if ym <= 0.001 or ym >= 0.999: continue
            if el >= ENTRY_DELAY:
                ok = True
                if ent and len(ent) >= max_entries: ok = False
                if ent and el < ent[-1]["t"] + 20: ok = False
                if ok and bc > btc_no and el <= 180 and ym <= yl:
                    ent.append({"s":"NO","ep":ym,"p":1-ym,"z":8,"t":el,"x":False,"bc":bc})
                elif ok and bc < -btc_yes and el <= 270 and ym >= yh:
                    ent.append({"s":"YES","ep":ym,"p":ym,"z":8,"t":el,"x":False,"bc":bc})
            if el < 285:
                for e in ent:
                    if e["x"]: continue
                    d = "YES" if e["ep"] >= 0.55 else "NO"
                    if d == "YES" and ym >= 0.94:
                        e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True
                    elif d == "NO" and ym <= 0.06:
                        e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True
                    if not e.get("x"):
                        s = sl_yes if d=="YES" else sl_no
                        if s and e.get("bc") is not None:
                            th = max(abs(e["bc"])*s, 10)
                            if d == "YES" and e["bc"] < 0 and bc < -th:
                                e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True
                            elif d == "NO" and e["bc"] > 0 and bc > th:
                                e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True
        for e in ent:
            if not e.get("x"):
                won=(st=="Up" and e["s"]=="YES") or (st=="Down" and e["s"]=="NO")
                e["pn"]=round((1.0-e["p"])*e["z"],2) if won else round(-e["p"]*e["z"],2)
                tp_st += 1; tp_pnl_st += e["pn"]
            tp_pnl += e.get("pn", 0)
            if e.get("pn", 0) > 0: tp_w += 1
            elif e.get("pn", 0) < 0: tp_l += 1
    tr = tp_w + tp_l
    return {"pnl": round(tp_pnl, 2), "wr": round(tp_w/tr*100, 1) if tr > 0 else 0, "trades": tr}

result_b = sweep_sim(3, 10, 0.30, 0.45, 3, 3, 2, all_w)
print(f"PnL: ${result_b['pnl']:+.2f}, WR: {result_b['wr']}%, Trades: {result_b['trades']}")

# ======================================================================
# METHOD C: final_run.py logic with max_entries=2 and same size=10
# final_run.py only enters ONE trade per window (max_entries=1 hardcoded)
# But let's try with max_entries=2 to match
# ======================================================================
print("\n=== METHOD C: final_run.py logic (max_entries=2, size=10) ===")

def final_run_logic(btc_yes, btc_no, yl, yh, sl_yes, sl_no, max_entries, wins, size):
    tp_pnl = 0; tp_w = 0; tp_l = 0
    tp_tp_n=0; tp_sl_n=0; tp_st_n=0
    tp_tp_p=0; tp_sl_p=0; tp_st_p=0
    for slug, ws, bt, ys, st in wins:
        op=bt[0][1]; en=[]; si=0; ym=0.5
        for ts,bp in bt:
            td=datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
            el=td-ws
            if el<0 or el>=WINDOW_LEN: continue
            bc=bp-op
            while si<len(ys) and ys[si][0]<=ts: ym=ys[si][1]; si+=1
            if ym<=0.001 or ym>=0.999: continue
            if el>=ENTRY_DELAY and len(en) < max_entries:
                # Check signal persist
                if en and el < en[-1]["t"] + 20: pass  # no new entry
                else:
                    if bc>btc_no and el<=180 and ym<=yl:
                        en.append({"s":"NO","ep":ym,"p":1-ym,"z":size,"t":el,"x":False})
                    elif bc<-btc_yes and el<=270 and ym>=yh:
                        en.append({"s":"YES","ep":ym,"p":ym,"z":size,"t":el,"x":False})
            if el<285 and en and not en[0]["x"]:
                e=en[0]; d="YES" if e["ep"]>=0.55 else "NO"
                th=0.85 if d=="YES" else 0.88
                if d=="YES" and ym>=th:
                    e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True; tp_tp_n+=1; tp_tp_p+=e["pn"]
                elif d=="NO" and ym<=1.0-th:
                    e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True; tp_tp_n+=1; tp_tp_p+=e["pn"]
                if not e.get("x"):
                    s=sl_yes if d=="YES" else sl_no
                    if e.get("ep") is not None:
                        th2=max(e["ep"]*s, 10)
                        if d=="YES" and e["ep"]>0.5 and bc<-th2:
                            e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True; tp_sl_n+=1; tp_sl_p+=e["pn"]
                        elif d=="NO" and e["ep"]<0.5 and bc>th2:
                            e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True; tp_sl_n+=1; tp_sl_p+=e["pn"]
        if en and not en[0].get("x"):
            won=(st=="Up" and en[0]["s"]=="YES") or (st=="Down" and en[0]["s"]=="NO")
            en[0]["pn"]=round((1.0-en[0]["p"])*en[0]["z"],2) if won else round(-en[0]["p"]*en[0]["z"],2)
            tp_st_n+=1; tp_st_p+=en[0]["pn"]
        if en:
            p=en[0].get("pn",0); tp_pnl+=p
            if p>0: tp_w+=1
            elif p<0: tp_l+=1
    tr=tp_w+tp_l
    return {
        "pnl": round(tp_pnl, 2), "wr": round(tp_w/tr*100, 1) if tr > 0 else 0, "trades": tr,
        "tp_n": tp_tp_n, "tp_p": tp_tp_p,
        "sl_n": tp_sl_n, "sl_p": tp_sl_p,
        "st_n": tp_st_n, "st_p": tp_st_p,
    }

result_c = final_run_logic(3, 10, 0.30, 0.45, 3, 3, 2, all_w, 10)
print(f"PnL: ${result_c['pnl']:+.2f}, WR: {result_c['wr']}%, Trades: {result_c['trades']}")
print(f"  TP: {result_c['tp_n']} trades ${result_c['tp_p']:+.2f}")
print(f"  SL: {result_c['sl_n']} trades ${result_c['sl_p']:+.2f}")
print(f"  Settle: {result_c['st_n']} trades ${result_c['st_p']:+.2f}")

# ======================================================================
# METHOD D: final_run.py ACTUAL (max_entries=1, size=10, bn=5, sl_yes=4)
# ======================================================================
print("\n=== METHOD D: final_run.py ACTUAL (Old params: BTC_Y=3, BTC_N=5, SL_Y=4, SL_N=3, max_entries=1) ===")
result_d = final_run_logic(3, 5, 0.25, 0.45, 4, 3, 1, all_w, 10)
print(f"PnL: ${result_d['pnl']:+.2f}, WR: {result_d['wr']}%, Trades: {result_d['trades']}")
print(f"  TP: {result_d['tp_n']} trades ${result_d['tp_p']:+.2f}")
print(f"  SL: {result_d['sl_n']} trades ${result_d['sl_p']:+.2f}")
print(f"  Settle: {result_d['st_n']} trades ${result_d['st_p']:+.2f}")

print("\n=== KEY DIFFERENCES ===")
print("compare_btc5m_params.py New: btc_no=10, size=10, max_entries=2, TP thresholds 0.85/0.88, SL uses entry_btc_change")
print("enhanced_sweep.py: btc_no=10, size=8, max_entries=2, TP at 0.94/0.06, SL uses entry_btc_change")
print("final_run.py: btc_no=5, size=10, max_entries=1, TP at 0.85/0.88, SL uses entry_yes*sl, bn=5 (not 10)")
