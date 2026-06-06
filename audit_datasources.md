# Polymarket Bot 数据源审计报告 v2

> 生成时间: 2026-05-02 21:45 UTC
> v2 修正: 移除错误的 Ethereum mainnet Chainlink 合约引用，改为 Polymarket RTDS WebSocket

---

## 数据源完整清单（修正版）

| 类别 | 数据源 | Endpoint | 使用位置 | 用途分类 | 实时/历史 | 风险 | 评估 |
|------|--------|----------|----------|----------|----------|:----:|:----:|
| **BTC信号** | Binance WebSocket | `wss://stream.binance.com:9443/ws/btcusdt@ticker` | realtime_trader.py | ⚡ **交易信号 — 正确** | 实时 | 🟡中 | 保留 |
| **BTC信号** | Binance REST | `https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT` | realtime_trader.py | ⚡ **交易信号 fallback — 正确** | 2s轮询 | 🟡中 | 保留 |
| **BTC信号** | Binance Klines | `https://api.binance.com/api/v3/klines` | dune/download_btc.py | 📊 **回测信号 — 可继续用** | 历史 | 🟢低 | 保留 |
| **BTC结算❌** | **Binance 误作结算** | `binance_btc_ticks` 表 | 全部3个backtest | ❌ **错误用作 settlement truth** | 历史 | 🔴高 | **P0: 替换** |
| **市场发现** | Gamma API | `https://gamma-api.polymarket.com/markets?slug={slug}` | realtime_trader.py | ✅ **市场发现 — 正确** | 实时 | 🟢低 | 保留 |
| **YES/NO价格** | CLOB V2 /midpoint | `https://clob.polymarket.com/midpoint?token_id={id}` | realtime_trader.py | ✅ **实时交易价 — 正确** | 0.5s | 🟢低 | 保留 |
| **订单簿** | CLOB V2 /book | `https://clob.polymarket.com/book?token_id={id}` | clob_client.py | ✅ **完整订单簿** | 实时 | 🟢低 | 保留 |
| **结算真相❌** | **Gamma outcomePrices 误用** | Gamma API `outcomePrices` 字段 | realtime_trader.py _discover_market | ❌ **这只是市场交易价，不是oracle结算价** | 实时 | 🟡中 | **P2: 标记** |
| **结算真相✅（缺失）** | **Polymarket RTDS Chainlink** | `wss://ws-live-data.polymarket.com` topic:`crypto_prices_chainlink` symbol:`btc/usd` | **不存在** | 🔴 **缺失 — 应作为 settlement truth** | 实时 | 🔴高 | **P0: 新增** |
| **链上** | Polygon RPC | `https://polygon-rpc.com` (可配置) | config.py | 🔑 **钱包交互** | 实时 | 🔴高 | P2 |
| **回测数据** | Dune Analytics | `https://api.dune.com/api/v1/` | dune/*.py | 📊 **历史成交量** | 历史 | 🟡中 | 保留 |
| **本地DB** | SQLite | `data/btc5m.db` — 4张表 | 几乎所有文件 | 🗄️ **数据仓库** | 混合 | 🟡中 | **P0: 加表** |
| **实时状态** | JSON | `logs/trader_state.json` | realtime_trader.py → dashboard_api.py | 📺 **Dashboard** | 0.5s | 🟢低 | 保留 |
| **交易日志** | JSONL | `logs/paper_trades.jsonl` | realtime_trader.py | 📝 **交易历史** | 事件驱动 | 🟢低 | **P1: 加字段** |

---

## POLYMARKET RTDS CHAINLINK — 待接入的关键数据源

### 连接信息

```
WebSocket: wss://ws-live-data.polymarket.com
Topic:     crypto_prices_chainlink
Symbol:    btc/usd
```

### 预期数据结构（需要验证）

```json
{
  "topic": "crypto_prices_chainlink",
  "symbol": "btc/usd",
  "value": 7845012345,
  "timestamp_ms": 1777742100000,
  "round_id": 12345,
  "latency_ms": 5
}
```

### 和普通 Chainlink Price Feed 的区别

| 特性 | Chainlink Price Feed (latestRoundData) | Chainlink Data Stream (Polymarket RTDS) |
|------|---------------------------------------|----------------------------------------|
| 更新方式 | 链上 pull — 你调 eth_call | WebSocket push — 服务端推送 |
| 延迟 | 30s-几分钟（取决于心跳和Gas） | 亚秒级 |
| 数据频率 | 每 ~1 分钟（价格变化时） | 价格变化即推送 |
| 适用场景 | 链上结算（defi） | 高频预测市场 |
| Polymarket使用 | ❌ | ✅ |

---

## P0 — 实盘前必须修复（先做最小改动）

### P0-A: 新增 Polymarket RTDS Chainlink BTC collector

**目标:** 通过 Polymarket 的 WebSocket 实时接收 Chainlink BTC/USD Data Stream。

**新增文件:** `src/chainlink_collector.py`

```python
# 最小实现框架（~80行）
class ChainlinkCollector:
    """
    连接 Polymarket RTDS WebSocket, 订阅 crypto_prices_chainlink / btc/usd.
    写入 chainlink_btc_ticks 表.
    
    WS: wss://ws-live-data.polymarket.com
    Topic: crypto_prices_chainlink
    Symbol: btc/usd
    """
    WS_URL = "wss://ws-live-data.polymarket.com"
    
    def __init__(self, db):
        self.db = db
        self.ws = None
        self.running = True
    
    def start(self):
        """启动 WS 连接并订阅"""
        ws = websocket.WebSocketApp(
            self.WS_URL,
            on_message=self._on_message,
            on_error=self._on_error,
        )
        # 连接后发送 subscribe 消息
        def on_open(ws):
            subscribe_msg = json.dumps({
                "type": "subscribe",
                "topic": "crypto_prices_chainlink",
                "symbols": ["btc/usd"]
            })
            ws.send(subscribe_msg)
        
        ws.on_open = on_open
        thread = threading.Thread(target=ws.run_forever, daemon=True)
        thread.start()
    
    def _on_message(self, ws, message):
        data = json.loads(message)
        # 解析 payload
        value = data.get("value")
        source_ts = data.get("timestamp_ms")
        received_ts = int(time.time() * 1000)
        latency = received_ts - source_ts if source_ts else None
        
        # 写入 chainlink_btc_ticks 表
        self.db.execute("""
            INSERT INTO chainlink_btc_ticks 
            (source, symbol, value, source_timestamp_ms, received_at_ms, latency_ms, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "polymarket_rtds_chainlink",
            "btc/usd",
            value,
            source_ts,
            received_ts,
            latency,
            message,
        ))
```

### P0-B: 新增 chainlink_btc_ticks 表

**在 db.py 或 migration 中添加:**

```sql
CREATE TABLE IF NOT EXISTS chainlink_btc_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                    -- 'polymarket_rtds_chainlink'
    symbol TEXT NOT NULL,                    -- 'btc/usd'
    value REAL,                              -- oracle price (already divided by 1e8)
    source_timestamp_ms INTEGER,             -- from RTDS message
    received_at_ms INTEGER,                  -- when we received it (time.time() * 1000)
    latency_ms INTEGER,                      -- received_at_ms - source_timestamp_ms
    raw_payload TEXT,                        -- original JSON message for debugging
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_chainlink_ticks_ts ON chainlink_btc_ticks(source_timestamp_ms);
```

### P0-C: 把 paper/backtest settlement 从 Binance 切换到 chainlink

**改动清单:**

| 文件 | 改动 |
|------|------|
| `src/realtime_trader.py` | `_record_entry()` 加写入当前 chainlink BTC 价格作为 entry_chainlink_price |
| `src/realtime_trader.py` | `run()` 结束时记录 chainlink BTC 价格作为 exit_chainlink_price |
| `logs/paper_trades.jsonl` | 每条 trade 加字段: `chainlink_entry`, `chainlink_exit`, `settlement_source` |
| `src/btc_paper_backtest.py` | 结算判定从 `binance_btc_ticks` → `chainlink_btc_ticks` |
| `src/honest_backtest.py` | 结算判定从 `binance_btc_ticks` → `chainlink_btc_ticks` |
| `dune/dune_backtest.py` | 结算判定从 `binance_btc_ticks` → `chainlink_btc_ticks` |

**paper_trades.jsonl 新增字段:**

```json
{
  "chainlink_entry": 7845012345,
  "chainlink_exit": null,
  "chainlink_entry_decimals": 8,
  "settlement_source": "polymarket_rtds_chainlink"
}
```

**PnL 计算逻辑切换到:**

```python
# 旧（错误）:
btc_up = binance_close > binance_open

# 新（正确）:
btc_up = chainlink_close > chainlink_open  # 使用 chainlink_btc_ticks
```

### P0-D: 保留 Binance 作为 signal（不改动）

- `_detect_signal()` 继续用 Binance WS 数据 — 不变
- 所有信号阈值（$8, $15, 0.45, 0.60）不变
- 止损逻辑（BTC-based ×3.0）不变 — 止损本就用 Binance（快速反应更重要）

---

## P1 — 正式测试前应该修

| 优先级 | 问题 | 修复 |
|:------:|------|------|
| **P1-A** | `paper_trades.jsonl` 缺少 order book 深度字段 | 每条记录加: `midpoint`, `best_bid`, `best_ask`, `spread`, `last_trade_price` |
| **P1-B** | `binance_btc_ticks` 时间戳格式不统一 | 统一为 Unix 毫秒 |
| **P1-C** | 回测标记旧 Binance-only 结果为 "signal-only, not settlement" | 输出时加警告 `⚠ settlement uses Binance, not Chainlink` |
| **P1-D** | `_poll_orderbook()` 只读 midpoint，不读 spread/depth | 改为同时读 `/book` 获取完整订单簿数据 |

---

## P2 — 后续优化

| 优先级 | 问题 | 修复 |
|:------:|------|------|
| **P2-A** | Gamma outcomePrices 被用于判断 market resolved | 改为只用 chainlink prices 判断结算，outcomePrices 仅辅助 |
| **P2-B** | Polygon RPC 公共节点风险 | 配置私有 RPC 或备用节点 |
| **P2-C** | 无数据源健康看板 | 新增各数据源的延迟/错误率监控面板 |
| **P2-D** | CLOB midpoint 在低流动性时不准 | 增加 spread 阈值过滤（spread > 5% 时告警） |

---

## 最小改动步骤（按顺序执行）

```
Step 1:  新增 chainlink_btc_ticks 表 (db.py migration)
Step 2:  新增 src/chainlink_collector.py (WS collector)
Step 3:  修改 live_collector.py 启动 chainlink collector
Step 4:  修改 realtime_trader.py _record_entry() 记录 chainlink entry price
Step 5:  修改 paper_trades.jsonl 输出加 chainlink 字段
Step 6:  修改 3 个 backtest 从 binance 切换到 chainlink 判定结算
Step 7:  运行 paper trade 1 个窗口，验证 chainlink 数据正确
Step 8:  (P1) 增加 order book 深度记录
Step 9:  (P1) 标记旧 backtest 结果
Step 10: (P2) Gamma outcomePrices 用途重新定义
```

---

## 架构图（修正版）

```
┌─────────────────────────────────────────────────────────────────┐
│  结算真相 (Settlement Truth)                                     │
│  ┌─────────────────────────────────────┐                        │
│  │ Polymarket RTDS WebSocket          │                        │
│  │ wss://ws-live-data.polymarket.com  │                        │
│  │ topic: crypto_prices_chainlink     │                        │
│  │ symbol: btc/usd                    │                        │
│  │ ↓                                  │                        │
│  │ chainlink_btc_ticks 表             │ ← P0 新增              │
│  │ → PnL 计算                          │                        │
│  │ → 回测 outcome                      │                        │
│  │ → post-trade 分析                    │                        │
│  └─────────────────────────────────────┘                        │
│                                                                  │
│  交易信号 (Trading Signal) — ✅ 不变                             │
│  ┌─────────────────────────────────────┐                        │
│  │ Binance WebSocket + REST            │                        │
│  │ → _detect_signal()                  │                        │
│  │ → BTC-based stop-loss               │                        │
│  │ → ENTRY_DELAY / signal thresholds   │                        │
│  └─────────────────────────────────────┘                        │
│                                                                  │
│  YES/NO 实时价格 — ✅ 不变                                        │
│  ┌─────────────────────────────────────┐                        │
│  │ CLOB V2 /midpoint                    │                        │
│  │ → _detect_signal() YES price        │                        │
│  │ → 记录 best_bid/ask/spread          │ ← P1 增强              │
│  └─────────────────────────────────────┘                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 需要 GPT 上网查证

| # | 问题 | 备注 |
|:-:|------|------|
| 1 | Polymarket RTDS WebSocket URL，topic 名，数据结构 | wss://ws-live-data.polymarket.com 是否有效？ |
| 2 | Chainlink Data Stream vs Price Feed 的区别确认 | 是否符合 Polymarket 的 "resolution source = Chainlink BTC/USD Data Stream" |
| 3 | RTDS btc/usd 的 value 是 8 decimals 还是 18？ | 确认 eth_call latestRoundData 的 answer 格式是否适用于 RTDS |
| 4 | Polymarket 的结算时间点：窗口结束时立刻结算，还是等 oracle 报告（~1min）？ | 影响 chainlink 数据采样窗口 |
| 5 | 有没有公开的 Chainlink Data Stream 历史数据 REST API？ | 不用 WebSocket 也能拉历史数据？ |
