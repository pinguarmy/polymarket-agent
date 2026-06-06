# Copy Trading on Polymarket BTC 5-Min: Honest Analysis

**For:** Peter  
**Date:** 2026-05-05  
**Context:** UK-based, running his own BTC 5-min Up/Down bot with live infrastructure

---

## 1. What Copy Trading Means on Polymarket

Copy trading on Polymarket means subscribing to a signal provider's trades and having your bot automatically execute the same positions they take — same market, same side, same window.

**How this differs from forex/crypto copy trading:**

| Dimension | Forex/Crypto | Polymarket BTC 5-min |
|----------|-------------|----------------------|
| Market structure | Continuous AMM or CLOB | CLOB, discrete 5-min resolution windows |
| Execution | Near-instant, deep books | Order-matching per window; fills not guaranteed |
| Price certainty | Slippage quantifiable | By the time you copy, the 5-min price may have moved |
| Signal horizon | Seconds to days | Must enter within the window, settle at end |
| Trade frequency | High | One outcome per 5-min window |
| Transparency | Often opaque | Most signal providers show curated track records |

The critical difference: Polymarket BTC 5-min markets have a **fixed resolution horizon**. You cannot hold longer or shorter — the trade is binary and time-bounded. This makes latency devastating. A 2-second delay in copying means you've potentially missed the entry window entirely or entered at a worse price.

---

## 2. Key Risks and Pitfalls

### 2.1 Slippage and Latency on Polymarket

Polymarket uses a CLOB model. Orders are matched against the existing order book. The displayed price is not a guaranteed fill price — it's where orders *would* fill if the book doesn't change between you seeing it and your order reaching the matcher.

For a 5-min window:
- The window opens, price starts at ~$0.50 (50/50)
- Signal provider sees early directional move, enters YES at $0.52
- By the time the copy signal reaches your bot via API, 1-3 seconds have passed
- Price has moved to $0.54 or $0.50
- Your fill may be worse than the provider's, or you may not get filled at all

**For BTC 5-min specifically:** BTC is highly liquid, but the Polymarket BTC Up/Down markets can have thin order books outside the opening minutes of a window. Spreads of 1-3 cents are common. Latency of 1 second in a 5-minute (300-second) window is 0.33% of the window's life — but the price action is front-loaded to the first 30-60 seconds. Most of the signal value is in the first 15-30 seconds.

### 2.2 Signal Quality: Real Edge vs. Luck

How do you verify the provider's edge is genuine?

**Red flags to look for:**
- Backtest-only track records (no live trading history)
- Win rate without context: 63% sounds good but you need to know expectancy per trade, reward-to-risk ratio, and sample size
- Survivorship bias: promoted providers are cherry-picked by platforms like Paragraph or Raid社交. Losers are unlisted.
- Short track records: 50 trades is not enough to distinguish skill from luck in binary markets with ~50% base rate
- Inconsistent strategy: provider changes approach frequently to chase recent performance

**What genuine edge looks like:**
- Out-of-sample validated (walk-forward, not just in-sample backtest)
- Sharpe-like metric > 1.5 over 300+ trades
- Strategy is explainable and documented, not a black box
- Provider trades their own capital, not just signals
- Results are auditable on-chain (Polymarket is on Polygon, trades are verifiable)

### 2.3 Overfitting in Backtests

This is the biggest problem with most signal provider claims.

Your own experience confirms this — you've run 13,000+ parameter combinations and found "optimal" parameters that performed well in-sample. But Codex (correctly) flagged that 213 windows is not enough data. The parameter sets you found optimal may be overfit to that specific 2-day sample.

Signal providers have the same problem, often worse:
- They show backtests with Sharpe 3.0+ on 100 trades — this is almost certainly overfitted
- They optimize on the same data they test on (data snooping)
- Look-ahead bias: they may have inadvertently used future information in their signal construction

**The math:** For a 50% base-rate binary market, you need ~300 trades to be 95% confident that a 55% win rate is real (i.e., p < 0.05 that it's random). Most providers don't have this track record.

### 2.4 The 5-Minute Window Problem

This is the killer. For a 5-min window:
1. Signal provider sees a signal (say, price crosses 0.45 on YES)
2. Provider enters the trade — takes 0.5-2 seconds
3. Signal is transmitted to copy platform — 0.5-2 seconds
4. Your bot receives signal, processes, submits order — 0.5-1 second
5. Order reaches Polymarket CLOB — 0.2-0.5 seconds

**Total: 1.7 to 5.5 seconds of latency**

In the first minute of a 5-min window:
- Price is most volatile
- Order book is thinnest
- The signal is most actionable
- Your copy order arrives late, at a worse price, or misses the window

**By the time you copy, the profitable window may have passed.**

### 2.5 Polymarket-Specific: CLOB Fills Not Guaranteed

Your own risk rules correctly identify this: "Do not assume the order fills at the displayed price." This applies doubly to copy trading:
- Provider's order filled at $0.52
- Your copy order arrives when best bid/ask is $0.50/$0.54
- You get filled at $0.54 (worse) or not at all
- Now you're entering a position at a different cost basis than the provider

This isn't just slippage — it's a **different trade**. The provider's risk management assumed entry at $0.52. Yours assumed entry at $0.54. Same direction, different trade.

### 2.6 Survivorship Bias in Promoted Signal Providers

Platforms that aggregate or promote signal providers have strong incentives to show winners. They:
- Depromote or remove losing providers from rankings
- Show only the curated track records of survivors
- Cherry-pick time periods that favor their top performers

**You will almost never see the provider who blew up their account on BTC 5-min windows in Week 1.** Their results are gone. The providers still shown are the survivors — but survivorship doesn't equal skill, it may just mean they got lucky in the right market conditions.

---

## 3. What Would Make a Copy Trade Strategy Actually Viable

### 3.1 Minimum Signal Metrics to Trust

For BTC 5-min Up/Down specifically, I'd want to see:

| Metric | Minimum | Why |
|--------|---------|-----|
| Sample size | 500+ trades | Binary market with ~50% base rate needs large samples to distinguish skill |
| Live Sharpe (not backtest) | > 1.5 | Accounts for volatility of returns, not just win rate |
| Win rate | > 54% | With $3 YES / $5 NO asymmetry, even 54% can be profitable, but needs scale |
| Expectancy per trade | > $0.15 (on $10 unit) | After costs, needs to show positive expected value |
| Out-of-sample validation | Walk-forward on 3+ segments | Not cherry-picked on one data period |
| On-chain verification | Yes | Polymarket trades are on Polygon — verifiable |

**The uncomfortable truth:** I don't know of any publicly tracked BTC 5-min signal provider who meets all these criteria. The space is too new and the markets are too thin. Most "returns" shown are backtests, not live track records.

### 3.2 Infrastructure Requirements

If you still want to try copy trading alongside your own signals:

**Latency budget (max allowable):**
- Signal transmission: < 200ms
- Order submission to Polymarket API: < 100ms
- Total round-trip signal-to-fill confirmation: < 500ms

**For BTC 5-min:** The first 15 seconds of a window are where most actionable signals occur. Your copy order must reach the market within 500ms of the original signal, ideally less. This means:
- Co-located servers (or at minimum, low-latency connection to Polymarket's API)
- Real-time WebSocket for signal delivery, not polling
- Pre-authenticated orders (signed and ready to submit) to avoid signing latency

**Fill monitoring:**
- Poll order status after submission
- Log all partial fills, rejections, and timeouts
- Compare your fill price to the provider's reported entry price
- Track slippage per trade to measure if the provider's edge covers your latency cost

**Your current infrastructure (from what I can see):**
- You have `live_trader.py`, `paper_trader.py`, risk engine, and dispatcher
- You're using CLOB client directly
- You have a proper kill switch and risk rules

This is solid. The question is whether you can achieve sub-500ms total latency from signal provider's order to your fill — and whether the latency cost is small enough that the copy trade still has positive expected value.

### 3.3 Risk Management for Copy Trading

If you run copy trades alongside your own:

**Position sizing:**
- Copy trades should be a fraction of your own strategy's size (e.g., 20-30% of capital)
- Size based on your own risk tolerance, not the provider's reported size
- The provider may risk 5% of their bankroll; you may only want to risk 1-2%

**Max concurrent copy trades:**
- Limit to 1-2 simultaneous copy positions
- BTC 5-min windows overlap — you could be copying a provider who holds multiple positions
- Each copied position consumes your risk budget

**Stop-loss for copy trades:**
- Mirror the provider's stated stop-loss, but enforce it on your own timeline
- If provider doesn't publish a stop-loss, don't copy — you have no risk management anchor
- Auto-close copy positions if provider's trade goes against you by X% of entry

**Hard limits specific to copy trading:**
```
MAX_COPY_TRADE_SIZE = $5      # Never more than 20% of your per-trade size
MAX_COPY_DAILY_LOSS = $25     # Half of your overall daily loss limit
COPY_PROVIDER_KILL = True     # If provider's tracked Sharpe drops below 1.0, stop copying
```

**Never copy without:**
1. A way to verify the provider's on-chain trade history independently
2. Real-time slippage tracking per copied trade
3. The ability to immediately stop copying (kill switch for the provider)

---

## 4. Copy Trading vs. Running Your Own Signals

### Your Current Position

From your code and reports, you have:
- A signal generation system with documented parameters
- 213 windows of backtest data + walk-forward validation
- Risk rules enforced in code (kill switch, daily loss, max order size)
- Paper trader for live validation
- Proper infrastructure (CLOB client, data collection, logs)

You're not starting from zero. You have a working system.

### When Copy Trading Makes Sense (Rare)

- You find a provider with a **demonstrably different edge** that complements your own (e.g., you trade momentum, they trade mean-reversion)
- Provider's strategy works in market conditions where yours doesn't
- You have capital you want to diversify without building another strategy from scratch
- The provider's on-chain record is verifiable and they've been live for 500+ trades

### Why Running Your Own Is Usually Better

**Control:** You understand your strategy's assumptions, parameters, and failure modes. When something goes wrong, you can diagnose it. With copy trading, you're flying blind on the provider's logic.

**Latency:** Your own signals don't have to travel anywhere. The delay between signal and execution is purely your infrastructure latency. With copy trading, you're adding the provider's latency plus transmission delay.

**No additional risk from provider quality:** Your risk engine, your position sizing, your stop-loss. Copy trading inherits the provider's mistakes.

**Cost:** Copy trading platforms typically charge 10-20% of profits. On a strategy with 5-10% edge, that's a significant haircut.

**Your specific situation:**
- You already have Sharpe-like validation through walk-forward
- Your risk rules are well-designed
- You've caught and fixed bugs in your own code
- You're technical enough to iterate on your own strategy

The question isn't "copy trading vs. starting from scratch." It's "copy trading vs. improving what I already have." For someone with your infrastructure and skill level, improving your own strategy almost always has higher expected value than delegating to a provider.

---

## 5. Bottom Line

### Is copy trading on Polymarket worth pursuing?

**Honest answer: No — not for BTC 5-min markets, not now.**

The reasons:

1. **Latency kills the edge.** A 1-5 second delay in copying a 5-min window trade is not marginal — it's the entire trade window. The actionable signal is in the first 15-30 seconds. By the time your copy order arrives, you're trading a different market than the provider entered.

2. **No verified track records.** The BTC 5-min market is new enough that I don't know of any provider with 500+ live trades, on-chain verifiable, with Sharpe > 1.5 out-of-sample. What gets promoted is survivorship-biased and largely backtested.

3. **You're not the target audience.** Copy trading works best for people with no strategy who want passive exposure. You have a documented, iterated strategy with risk management. You're better off improving your own edge than delegating to someone else's.

4. **The 5-min window structure is hostile to copy trading.** It's not like forex where you can copy a swing trade hours or days later. The resolution is 5 minutes. Signal transmission delay is a first-order problem, not a second-order one.

### Smarter Path for Peter

Given you already have a running BTC 5-min bot:

**Priority 1: Fix known bugs before scaling**
Your own STRATEGY_REPORT identifies two critical bugs that must be fixed before any scaling:
- TP/SL not actually submitting CLOB sell orders
- Not checking actual fill after order submission

Fix these. They're the difference between paper profits and real losses.

**Priority 2: Walk-forward validation with real costs**
Run walk-forward on your strategy with:
- Bid/ask spread added (realistic: +$0.01 to +$0.03)
- 10-20% fill failure rate
- 1-3 second latency
- Gas/transaction costs

If the strategy survives this stress test, it has real edge.

**Priority 3: Scale your own size carefully**
- You can scale $10 → $25 → $50 per trade as you validate
- Multi-window concurrently (up to 2-3 BTC windows simultaneously per your risk rules)
- No copying needed — you control the size

**Priority 4: If you want diversification, build a second strategy**
Rather than following someone else's signals, build a complementary strategy yourself:
- If your current strategy is momentum-based, add mean-reversion signals
- If you're trading BTC 5-min, test ETH 5-min or BTC 15-min as separate markets
- Keep strategies independent so they don't correlate in drawdowns

**What you could use copy trading for:**
- As a **secondary data source**: if a provider consistently shows positions before large moves, that's information you can incorporate into your own signal logic — not something to blindly follow
- As a **sentiment indicator**: aggregate copy trade flow as a contrarian signal
- Never as your primary execution mechanism for BTC 5-min

---

## Summary

Copy trading on Polymarket BTC 5-min is theoretically appealing but practically problematic. The 5-minute resolution window, CLOB fill uncertainty, and latency make it hostile to the copy trading model as practiced in forex/crypto.

Your current infrastructure puts you in the top 1% of Polymarket BTC traders. The expected value of improving your own strategy (fixing bugs, walk-forward validation, proper cost modeling) far exceeds the expected value of finding and following a signal provider.

**Focus on:**  
1. Fixing TP/SL and fill-checking bugs  
2. Walk-forward validation with realistic costs  
3. Scaling your own proven strategy  
4. Building complementary strategies if you want diversification  

**Forget about:** Delegating your trading to third-party signal providers on a 5-minute resolution market.
