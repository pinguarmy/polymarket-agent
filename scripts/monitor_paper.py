#!/usr/bin/env python3
"""Monitor paper trading output and track signals."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOGS_DIR = Path(os.environ.get("HOME", "~")) / "Projects/polymarket-agent" / "logs"
TRACKER_FILE = LOGS_DIR / "paper_tracker.json"

def get_output():
    """Get recent trader output."""
    result = subprocess.run(
        ["tail", "-50", str(LOGS_DIR / "realtime_trader.log")],
        capture_output=True, text=True, timeout=5
    )
    return result.stdout

def check_for_new_signals(output, seen_lines):
    """Parse output for new DRY RUN signals."""
    signals = []
    for line in output.split("\n"):
        line_stripped = line.strip()
        if not line_stripped or line_stripped in seen_lines:
            continue
        seen_lines.add(line_stripped)
        if "DRY:" in line or "DRY RUN" in line.upper():
            signals.append(line_stripped)
    return signals, seen_lines

def main():
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_file = LOGS_DIR / "realtime_trader.log"
    
    # Read existing signals
    seen_lines = set()
    if log_file.exists():
        with open(log_file) as f:
            for line in f.read().split("\n"):
                seen_lines.add(line.strip())
    
    output = get_output()
    signals, seen_lines = check_for_new_signals(output, seen_lines)
    
    # Also write current output to a persistent log
    with open(log_file, "a") as f:
        f.write(f"\n--- poll at {datetime.now().isoformat()} ---\n")
        f.write(output)
    
    if signals:
        print("NEW_SIGNALS:" + json.dumps(signals))
    else:
        print("NO_NEW_SIGNALS")
    
    # Also check trader status
    result = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True, timeout=5
    )
    trader_running = "realtime_trader.py" in result.stdout
    print(f"TRADER_RUNNING:{trader_running}")

if __name__ == "__main__":
    main()
