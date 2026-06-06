#!/usr/bin/env python3
"""Every 20-minute monitoring script.

Analyzes the last 20 minutes of trader data, checks strategy effectiveness,
and sends a concise report.
"""

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT / "logs" / "realtime_trader.log"
DB_FILE = PROJECT / "data" / "btc5m.db"


def read_trader_log(n_lines=5000):
    """Read the last N lines of the trader log."""
    if not LOG_FILE.exists():
        return []
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-n_lines:]
    except Exception as e:
        return [f"ERROR reading log: {e}"]


def analyze_last_20min(lines):
    """Analyze the last ~20 minutes of trader data."""
    report = {
        "markets_seen": set(),
        "signals_fired": [],
        "btc_moves": [],
        "yes_prices": {},
        "btc_yes_aligned": [],  # Track BTC-YES alignment per market: "up", "down", "opposite", "mixed"
        "errors": [],
        "time_span": None,
    }
    
    current_market = None
    market_start = None
    
    # Per-market data for alignment analysis
    market_btc_change = {}  # market -> last BTC change
    market_yes_price = {}   # market -> last YES price
    
    for line in lines:
        # Track markets
        m = re.search(r"NEW MARKET:\s*(\S+)", line)
        if m:
            current_market = m.group(1)
            report["markets_seen"].add(current_market)
        
        # Track BTC prices and changes
        m = re.search(r"BTC=\$?([\d.]+).*Δ\$?([+-]?\d+)", line)
        if m:
            btc = float(m.group(1))
            change = int(m.group(2))
            report["btc_moves"].append(change)
            if current_market:
                market_btc_change[current_market] = change
        
        # Track YES prices
        m = re.search(r"YES[☆★]\s*([\d.]+)", line)
        if m:
            price = float(m.group(1))
            if current_market:
                if current_market not in report["yes_prices"]:
                    report["yes_prices"][current_market] = []
                report["yes_prices"][current_market].append(price)
                market_yes_price[current_market] = price
        
        # Track signals
        if "DRY:" in line and "FOLLOW" in line:
            report["signals_fired"].append(line.strip()[:120])
        
        # Track errors
        if "ERROR" in line or "RISK BLOCKED" in line or "error" in line.lower():
            report["errors"].append(line.strip()[:100])
    
    # Compute BTC-YES alignment for each market
    for market in report["markets_seen"]:
        btc_ch = market_btc_change.get(market, 0)
        yes_p = market_yes_price.get(market, 0.5)
        
        if btc_ch > 0 and yes_p > 0.5:
            alignment = "aligned_up"  # BTC up + YES up = aligned
        elif btc_ch < 0 and yes_p < 0.5:
            alignment = "aligned_down"  # BTC down + YES down = aligned
        elif btc_ch > 0 and yes_p < 0.45:
            alignment = "manipulation_up"  # BTC up + YES down (>0.55 threshold inverted) = manipulation
        elif btc_ch < 0 and yes_p > 0.55:
            alignment = "manipulation_down"  # BTC down + YES up (<0.45 threshold inverted) = manipulation
        elif abs(btc_ch) < 5:
            alignment = "btc_too_small"  # BTC change too small to act on
        else:
            alignment = "mixed"
        
        report["btc_yes_aligned"].append({
            "market": market,
            "btc_change": btc_ch,
            "yes_price": yes_p,
            "alignment": alignment
        })
    
    return report


def compute_stats(report):
    """Compute meaningful stats from the raw data."""
    stats = {}
    
    # BTC analysis
    moves = report["btc_moves"]
    if moves:
        stats["btc_min"] = min(moves)
        stats["btc_max"] = max(moves)
        stats["btc_avg"] = sum(moves) / len(moves)
        # Count big moves (>= $5)
        big_moves = [m for m in moves if abs(m) >= 5]
        stats["big_moves"] = len(big_moves)
        stats["total_samples"] = len(moves)
    else:
        stats["btc_min"] = stats["btc_max"] = stats["btc_avg"] = stats["big_moves"] = 0
        stats["total_samples"] = 0
    
    # Market analysis
    stats["markets"] = len(report["markets_seen"])
    markets_list = list(report["markets_seen"])[:5]
    stats["market_names"] = markets_list
    
    # YES price analysis per market
    yes_snapshots = 0
    yes_volatile_markets = 0
    for market, prices in report["yes_prices"].items():
        yes_snapshots += len(prices)
        if len(prices) >= 3:
            p_min = min(prices)
            p_max = max(prices)
            if p_max - p_min >= 0.05:  # 5¢ movement = volatile
                yes_volatile_markets += 1
    
    stats["yes_snapshots"] = yes_snapshots
    stats["yes_volatile_markets"] = yes_volatile_markets
    
    # Signals
    stats["signals"] = len(report["signals_fired"])
    stats["signal_details"] = report["signals_fired"][-5:]  # last 5
    
    # Errors
    stats["errors"] = len(report["errors"])
    stats["error_details"] = report["errors"][-3:]  # last 3
    
    # Manipulation detection
    manip_signals = 0
    for s in report["signals_fired"]:
        if "manip" in s.lower():
            manip_signals += 1
    stats["manip_signals"] = manip_signals
    
    # BTC-YES alignment analysis
    stats["btc_yes_aligned"] = report["btc_yes_aligned"]
    aligned_count = sum(1 for a in report["btc_yes_aligned"] if a["alignment"] in ("aligned_up", "aligned_down"))
    manip_count = sum(1 for a in report["btc_yes_aligned"] if a["alignment"] in ("manipulation_up", "manipulation_down"))
    stats["btc_yes_aligned_count"] = aligned_count
    stats["btc_yes_manip_count"] = manip_count
    
    # Track trade entries from paper_trades.jsonl
    trade_path = PROJECT / "logs" / "paper_trades.jsonl"
    stats["trade_entries"] = 0
    stats["unique_markets_traded"] = set()
    stats["recent_entries"] = []
    if trade_path.exists():
        try:
            lines = trade_path.read_text().strip().splitlines()
            entries = [json.loads(l) for l in lines if l.strip()]
            # Filter to last 20 min
            cutoff = datetime.now(timezone.utc).timestamp() - 1200
            recent = [e for e in entries if e.get("ts","") >= datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()]
            stats["trade_entries"] = len(recent)
            stats["unique_markets_traded"] = set(e.get("slug","") for e in recent)
            stats["recent_entries"] = recent[-5:]
        except Exception:
            pass
    
    return stats


def generate_report(stats):
    """Generate a concise human-readable report."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    
    lines = []
    lines.append(f"🤖 BTC 5min Trader Report — {now}")
    lines.append(f"{'='*40}")
    lines.append("")
    
    # BTC status
    lines.append(f"📊 BTC: {stats['btc_avg']:.1f} avg change")
    lines.append(f"   Range: ${stats['btc_min']:+d} to ${stats['btc_max']:+d}")
    lines.append(f"   Big moves (≥$5): {stats['big_moves']}/{stats['total_samples']}")
    lines.append("")
    
    # Markets
    lines.append(f"🎯 Markets tracked: {stats['markets']}")
    for m in stats["market_names"]:
        ts = m.split("-")[-1]
        h = int(ts) % 86400 // 3600
        mi = int(ts) % 3600 // 60
        lines.append(f"   {h:02d}:{mi:02d}")
    lines.append("")
    
    # YES price volatility
    lines.append(f"📈 YES snapshots: {stats['yes_snapshots']}")
    lines.append(f"   Volatile markets: {stats['yes_volatile_markets']}")
    lines.append("")
    
    # Signals
    lines.append(f"💡 Signals fired: {stats['signals']}")
    lines.append(f"   Manipulation signals: {stats['manip_signals']}")
    if stats["signal_details"]:
        lines.append("   Recent:")
        for s in stats["signal_details"][-3:]:
            lines.append(f"   • {s[:80]}")
    lines.append("")
    
    # Paper trade entries
    if stats["trade_entries"] > 0:
        lines.append(f"📝 Paper trades: {stats['trade_entries']}")
        lines.append(f"   Markets traded: {len(stats['unique_markets_traded'])}")
        if stats["recent_entries"]:
            for e in stats["recent_entries"][-3:]:
                side = e.get("side","?")
                price = e.get("entry_price",0)
                btc = e.get("btc_change",0)
                yes = e.get("yes_price",0)
                lines.append(f"   • {side} @ ${price:.2f} | BTC=${btc:+.0f} | YES={yes:.3f}")
        lines.append("")
    
    # Errors
    if stats["errors"] > 0:
        lines.append(f"⚠️ Errors: {stats['errors']}")
        if stats["error_details"]:
            lines.append("   Last:")
            for e in stats["error_details"]:
                lines.append(f"   • {e[:80]}")
        lines.append("")
    
    # Strategy recommendation
    if stats["signals"] > 0:
        lines.append("✅ Strategy active — signals detected")
    elif stats["btc_yes_aligned"]:
        # Analyze alignment when signals are 0
        aligned = [a for a in stats["btc_yes_aligned"] if a["alignment"] in ("aligned_up", "aligned_down")]
        manip = [a for a in stats["btc_yes_aligned"] if a["alignment"] in ("manipulation_up", "manipulation_down")]
        
        if aligned:
            dirs = []
            for a in aligned:
                d = "↑" if a["alignment"] == "aligned_up" else "↓"
                dirs.append(f"BTC${a['btc_change']:+d}{d}→YES={a['yes_price']:.3f}")
            lines.append(f"🟢 BTC-YES对齐 ({len(aligned)}市场): {', '.join(dirs)}")
            lines.append("   市场方向一致，无操纵信号")
        if manip:
            dirs = []
            for a in manip:
                d = "↑" if a["alignment"] == "manipulation_up" else "↓"
                dirs.append(f"BTC${a['btc_change']:+d}{d}→YES={a['yes_price']:.3f}")
            lines.append(f"🔴 BTC-YES背离 ({len(manip)}市场): {', '.join(dirs)}")
            lines.append("   方向背离但信号为0，继续观察")
        if not aligned and not manip:
            lines.append("🟡 BTC大幅波动但无信号 — 继续监控")
    elif stats["big_moves"] > 0 and stats["yes_volatile_markets"] > 0:
        lines.append("🟡 BTC moving + YES volatile — manipulation possible, watching...")
    elif stats["big_moves"] > 0:
        lines.append("🟡 BTC moving but YES stable — no manipulation yet")
    else:
        lines.append("⚪ Low volatility — waiting for BTC to move ≥$5")
    
    return "\n".join(lines)


def main():
    lines = read_trader_log()
    if not lines or (len(lines) == 1 and lines[0].startswith("ERROR")):
        print("⚠️ No trader data in last 20 min")
        return
    
    report = analyze_last_20min(lines)
    stats = compute_stats(report)
    summary = generate_report(stats)
    print(summary)


if __name__ == "__main__":
    main()
