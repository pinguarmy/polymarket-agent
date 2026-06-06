# Polymarket Bot 数据源正确性审计 — 完整报告

> 生成时间: 2026-05-02 21:45 UTC
> 审计方法: 实际 API 请求 + 数据库读取 + 代码扫描
> 范围: 仅数据获取正确性，不涉及策略

---

## A. BTC Settlement 数据 — Chainlink RTDS

### 连接信息

| 项目 | 值 |
|------|-----|
| WebSocket | `wss://ws-live-data.polymarket.com` |
| 订阅格式 | `{"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink","type":"*","filters":"{\"symbol\":\"btc/usd\"}"}]}` |
| 官方确认 | ✅ Gamma `resolutionSource` = `https://data.chain.link/streams/btc-usd` |

### 实际 payload 结构

**订阅确认 (batch 历史数据):**
```json
{
  "payload": {
    "data": [
      {"timestamp": 1777754931000, "value": 78416.49991887489},
      ...
      {"timestamp": 1777754989000, "value": 78415.20354044138}
    ],
    "symbol": "btc/usd"
  },
  "timestamp": 1777754990490,
  "topic": "crypto_prices",
  "type": "subscribe"
}
```

**实时 update:**
```json
{
  "connection_id": "cwR5Ud6ZrPECIVw=",
  "payload": {
    "full_accuracy_value": "78411240915500000000000",
    "symbol": "btc/usd",
    "timestamp": 1777754991000,
    "value": 78411.2409155
  },
  "timestamp": 1777754992704,
  "topic": "crypto_prices_chainlink",
  "type": "update"
}
```

### 字段解析表

| 字段 | 实际含义 | 当前解析 | 是否正确 | 风险 | 修复建议 |
|------|---------|---------|:--------:|:----:|---------|
| `payload.value` | Chainlink oracle BTC/USD price (float, USD) | `value_raw = float(value)` | ✅ 正确 | 🟢低 | 不变 |
| `payload.full_accuracy_value` | 高精度 int 字符串 (23-25位) | 未保存 | ⚠️ 已丢弃 | 🟡中 | 应保存到 `raw_payload` 字段 — 已存 |
| `payload.timestamp` | **Oracle source timestamp** (Unix ms) | `source_timestamp_ms` | ✅ 正确 | 🟢低 | 不变 |
| `top-level timestamp` | **Polymarket server收到时间** (Unix ms) | 未使用 | ✅ 正确 | 🟢低 | 如果需要可以记录 |
| `received_at_ms` | **我们收到的时间** (time.time()*1000) | `received_at_ms` | ✅ 正确 | 🟢低 | 不变 |
| `latency_ms` | `received_at_ms - source_timestamp_ms` | `latency_ms` | ✅ 正确 | 🟢低 | 不变 |
| `connection_id` | WS session ID | 未保存 | ✅ 无害 | 🟢低 | 可忽略 |
| `type` | "subscribe" 或 "update" | `message_type` | ⚠️ 未保存到DB | 🟡中 | `message_type` 应该保存 |
| `topic` | "crypto_prices" (batch) 或 "crypto_prices_chainlink" (update) | `topic` | ✅ 正确 | 🟢低 | 不变 |

### 数据特性

| 特性 | 是否 | 说明 |
|------|:----:|------|
| value 是 float | ✅ | 如 78411.24，已经是 USD 价格 |
| 需要 /1e8 | ❌ | 不需要，已经是 float |
| batch + update 混合 | ✅ | 第一条是 batch（~58条历史），后续是逐条 update |
| 重复 tick | ❌ | 每次 value 都不同（19/19 次变化），无需显式去重 |
| 去重 key | N/A | 可以使用 `(source_timestamp_ms, value)` 但实测无重复 |
| 适合 settlement | ✅ | 官方 resolution source |
| 适合高频 signal | ❌ | 延迟 ~1.3s，不如 Binance (<100ms) |
| 适合历史 backtest | ❌ | RTDS 只保留最近约 58 条历史。需要额外历史数据源 |

---

## B. BTC Lead Signal 数据 — Binance

### 连接信息

| 数据源 | Endpoint | 使用位置 | 使用的价格字段 | 正确？ |
|--------|----------|---------|---------------|:------:|
| **WebSocket (实时)** | `wss://stream.binance.com:9443/ws/btcusdt@ticker` | `realtime_trader.py:59` | `c` (last price) | ✅ |
| **REST fallback** | `https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT` | `realtime_trader.py:61` | `price` (last price) | ✅ |
| **Klines (历史)** | `https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m` | `dune/download_btc.py:19` | `candle close` (index 4) | ✅ (仅历史回测) |

### WS payload 字段

| 字段 | 含义 | 当前解析 |
|------|------|---------|
| `c` | Last price (string) | `float(data.get('c', 0))` |
| `E` | Event time (ms) | 未使用 |
| `C` | Close time | 未使用 |
| `s` | Symbol | 未记录 |

### 字段分析

| 字段 | 当前使用 | 是否正确 | 风险 | 修复建议 |
|------|---------|:--------:|:----:|---------|
| BTC last price for signal | `self.btc_price` | ✅ 正确 | 🟢低 | 用于 signal detection 的正确选择 |
| BTC open price | REST at market discovery | ⚠️ 可能有延迟 | 🟡中 | 市场发现时 REST 取 price，但可能比真实 window start 晚几秒 |
| WS event timestamp | 未记录 | ❌ 缺失 | 🟡中 | 应该记录 E 字段（事件时间）以便和 Chainlink 时间戳对齐 |
| Binance → settlement | backtest 中误用 | ❌ 错误 | 🔴高 | **P0: 必须改为 Chainlink** |
| Klines → settlement | 同样误用 | ❌ 错误 | 🔴高 | **P0: 必须改为 Chainlink** |

### Binance vs Chainlink 对齐

| 维度 | Binance WS | Chainlink RTDS | 可对齐？ |
|------|-----------|---------------|:--------:|
| 价格类型 | Last trade price | Oracle price | 不同概念 |
| 更新频率 | ~100ms | ~1s | 不同频率 |
| 延迟 | <100ms | ~1.3s | Binance 更快 |
| 时间戳 | Event time `E` | Oracle `payload.timestamp` | 对齐需加 Binance event time 记录 |

**结论：当前 Binance event time 未记录，数据流的时间戳对齐是不可能的。** 需要加 `E` 字段记录。

---

## C. Polymarket Market Discovery 数据

### 当前实现

| 步骤 | 实现 | 文件/行 |
|------|------|---------|
| 生成 slug | `f"btc-updown-5m-{(now_ts//300)*300}"` | `realtime_trader.py:226` |
| 查询 Gamma | `GET /markets?slug={slug}` | `realtime_trader.py:224-243` |
| 解析 clobTokenIds | `m.get("clobTokenIds", [])` | `realtime_trader.py:238-240` |
| 解析 outcomePrices | `m.get("outcomePrices")` | `realtime_trader.py:262-270` |
| 检查 resolved | `float(op[0]) in (0.0, 1.0)` | ✅ |

### 关键发现 — Gamma metadata 字段

| 字段 | 值 (示例市场) | 含义 | 当前是否使用 |
|------|-------------|------|:-----------:|
| `resolutionSource` | `https://data.chain.link/streams/btc-usd` | 官方结算数据源 URL | ❌ 未使用 |
| `outcomePrices` | `["0.485","0.515"]` | **条件代币价格**（不是 oracle 价格） | ✅ 用于 resolved 检查 |
| `outcomes` | `["Up", "Down"]` | 结果名称 | ❌ 未使用 |
| `bestBid` | `0.48` | **条件代币** best bid | ❌ 未使用 |
| `bestAsk` | `0.49` | **条件代币** best ask | ❌ 未使用 |
| `lastTradePrice` | `0.5` | 最后成交价 | ❌ 未使用 |
| `spread` | `0.01` | 条件代币 spread | ❌ 未使用 |
| `closed` | `False` | 是否关闭 | ✅ 用于活跃性检查 |
| `active` | `True` | 是否活跃 | ✅ 用于活跃性检查 |
| `endDate` / `endDateIso` | ISO timestamp | 结算时间 | ❌ 未使用 |
| `startDate` / `startDateIso` | ISO timestamp | 市场开始时间 | ❌ 未使用 |
| `conditionId` | `0xaccea...` | 条件 ID | ✅ 存储 |
| `negRisk` | `False` | 是否为负风险市场 | ❌ 未使用 |
| `orderMinSize` | `5` | 最小订单大小 | ❌ 未使用 |
| `volume` | `778.83` | 成交量 | ❌ 未使用 |

### 字段正确性

| 字段 | 当前解析 | 是否正确 | 风险 | 修复建议 |
|------|---------|:--------:|:----:|---------|
| slug 生成 | `(now_ts//300)*300` | ✅ 正确 | 🟢低 | 不变 |
| clobTokenIds[0] = YES | 是 | ✅ 正确 | 🟢低 | 已验证 CTF token |
| clobTokenIds[1] = NO | 是 | ✅ 正确 | 🟢低 | 已验证 |
| outcomePrices 用于 resolved | `float(op[0]) in (0,1)` | ✅ 正确但**不要用于 settlement** | 🟡中 | 只检查是否已结算，不变 |
| outcomePrices 当 oracle price | 未做 | ✅ 正确做法 | — | 已确认不能替代 Chainlink |
| `resolutionSource` | 未使用 | ❌ 丢失 | 🟢低 | 可以记录到 market 表 |
| `endDate` | 未使用 | ❌ 丢失 | 🟡中 | 可用于精确 settlement 时间 |
| Gamma 数据延迟 | `updatedAt` 可用 | ❌ 未检查 | 🟡中 | 可加 freshness check |

---

## D. CLOB Orderbook / Execution Price 数据

### Endpoint 实际 payload

**`/midpoint?token_id={id}`:**
```json
{"mid": "0.685"}
```
- mid 是字符串
- 是 (best_bid + best_ask) / 2

**`/price?token_id={id}&side=BUY`:**
```json
{"price": "0.68"}
```
- side=BUY → price = best ask (你买入的价格)
- 返回字符串

**`/price?token_id={id}&side=SELL`:**
```json
{"price": "0.69"}
```
- side=SELL → price = best bid (你卖出的价格)
- 返回字符串

**`/book?token_id={id}`:**
```json
{
  "market": "0x...",
  "asset_id": "0x...",
  "timestamp": "1777756602023",
  "bids": [{"price": "0.01", "size": "7242.29"}, ...],
  "asks": [{"price": "0.99", "size": "6082.16"}, ...],
  "min_order_size": "5",
  "tick_size": "0.01",
  "neg_risk": false,
  "last_trade_price": "0.690"
}
```
**关键：bids/asks 是 dicts!** `{"price": "...", "size": "..."}` 不是 `["price", "size"]`

**`/last-trade-price?token_id={id}`:**
```json
{"price": "0.69", "side": "BUY"}
```
- side = trade direction (BUY = market buy, SELL = market sell)

### 字段正确性表

| 字段 | 当前解析 | 是否正确 | 风险 | 修复建议 |
|------|---------|:--------:|:----:|---------|
| `/midpoint` mid | `float(data.get("mid",0))` | ✅ 正确 | 🟡中 | 但 midpoint ≠ 成交价 |
| midpoint 当成交价 | `yes_bid=yes_ask=mid` | ❌ 错误 | 🔴高 | **买入用 best_ask, 卖出用 best_bid** |
| `/price` side=BUY | 未使用 | — | — | 应作为模拟买入价 |
| `/price` side=SELL | 未使用 | — | — | 应作为模拟卖出价 |
| `/book` bids[0].price | 未使用 | — | 🔴高 | 缺少 spread/depth 过滤 |
| `/book` asks[0].price | 未使用 | — | 🔴高 | 同上 |
| `/book` bids/asks 格式 | 未使用 | — | 🔴中 | 代码可能假设 list of tuples（之前有 bug） |
| `/book timestamp` | 未使用 | — | 🟡中 | 可用于 CLOB 数据新鲜度 |
| `last-trade-price` | 未使用 | — | 🟡中 | 可用于真实成交价验证 |
| spread 计算 | 未实现 | ❌ 缺失 | 🔴高 | **应该作为入场条件** |
| depth 计算 | 未实现 | ❌ 缺失 | 🟡中 | 低深度时不应交易 |

### 实测 spread 示例

| 市场 | best_bid | best_ask | spread | 状态 |
|------|---------|---------|:------:|:----:|
| YES token (21:15 ET) | 0.01 | 0.99 | **98%** | 🔴极差流动性 — 不应交易 |
| 正常流动性时 | ~0.48 | ~0.52 | ~4% | 🟡一般 |

---

## E. Trade History / Historical Market Data

| 数据源 | 数据类型 | 实时/历史 | 是否官方 | 延迟 | 适合回测？ | 风险 |
|--------|---------|:--------:|:-------:|:----:|:--------:|:----:|
| CLOB `/last-trade-price` | 单条最近成交 | 实时 | 官方 | 实时 | ❌ 只有最新一条 | 🟢低 |
| CLOB `/prices-history` | 历史 midpoint | 历史 | 官方 | 分钟级 | ⚠️ midpoint 不是真实成交 | 🟡中 |
| Gamma `lastTradePrice` | 最后成交价 | 近实时 | 官方 | 数秒 | ❌ 只有最新值 | 🟢低 |
| Dune Analytics | 历史 trade/volume | 历史 | 外部 | 数小时~天 | ✅ 有真实成交价 | 🟡中 |
| CSV (dune_april_full) | 4月成交数据 | 历史 | 外部 | 固定 | ✅ 但可能过期 | 🟡中 |
| paper_trades.jsonl | 本地模拟成交 | 实时 | 本地 | 写入时 | ⚠️ 用 midpoint，非真实 | 🔴高 |

---

## F. Wallet / Position / Execution 数据

| 字段 | 当前来源 | 是否真实 | 风险 | 修复建议 |
|------|---------|:-------:|:----:|---------|
| pUSD 余额 | 硬编码 $254 | ❌ 过期值 | 🔴高 | 应从 CLOB API 或 RPC 读取 |
| 仓位大小 | SCALE_SIZE=$15 | ✅ 参数 | 🟢低 | 不变 |
| 入场价格 | `/midpoint` | ❌ 不一定是成交价 | 🔴高 | **应改为 best ask/bid** |
| 出场价格 (止损) | midpoint | ❌ 同上 | 🔴高 | **应改为 best bid/ask** |
| fees | 无 | ❌ 缺失 | 🟡中 | 实盘前必须加 |
| slippage | 无 | ❌ 缺失 | 🟡中 | 低流动性时显著 |

---

## G. Local Storage / Database 数据

| 表名 | 存储内容 | 时间戳格式 | raw_payload | 去重键 | 风险 |
|------|---------|:--------:|:----------:|:------:|:----:|
| `markets` | 市场元数据 | ISO TEXT | ❌ | slug | 🟢低 |
| `polymarket_price_snapshots` | 价格快照 | ISO TEXT | ❌ | autoinc | 🟢低 |
| `polymarket_trades` | 成交记录 | ISO TEXT | ❌ | autoinc | 🟢低 |
| `binance_btc_ticks` | BTC ticks | ISO TEXT（混合） | ❌ | autoinc | 🟡中 |
| `chainlink_btc_ticks` | Chainlink oracle 价格 | Unix ms INT | ✅ 保存 | autoinc | ✅ 推荐格式 |
| `paper_trades` | paper 交易记录 | ISO TEXT | ❌ | autoinc | 🟡中 |

**chainlink_btc_ticks 是唯一保存 raw_payload 的表**，这是正确做法。

---

## 最终输出

### 1. 数据源正确性总表

| 数据类别 | 当前来源 | 官方/正确来源 | 当前正确？ | 可用于 signal? | 可用于 settlement? | 可用于 execution? | 可用于 backtest? | 风险 | 修复优先级 |
|---------|---------|-------------|:---------:|:-------------:|:-----------------:|:----------------:|:----------------:|:----:|:---------:|
| **BTC settlement** | Binance (错误) | **Chainlink RTDS** | ❌ 错误 | ❌ | ✅ | ❌ | ⚠️ (需历史) | 🔴高 | **P0** |
| **BTC signal** | Binance WS | Binance WS | ✅ 正确 | ✅ | ❌ | ❌ | ✅ (作为 signal) | 🟢低 | — |
| **Market discovery** | Gamma slug | Gamma slug | ✅ 正确 | ❌ | ❌ | ❌ | ✅ | 🟢低 | — |
| **YES/NO 价格 (信号)** | CLOB /midpoint | CLOB /midpoint | ⚠️ 部分正确 | ✅ | ❌ | ❌ | ⚠️ | 🟡中 | P1 |
| **YES/NO 价格 (执行)** | midpoint 当成交价 | **CLOB best ask/bid** | ❌ 错误 | ❌ | ❌ | ✅ | ❌ | 🔴高 | **P1** |
| **Spread/depth** | 未实现 | CLOB /book | ❌ 缺失 | ❌ | ❌ | ✅ | ❌ | 🔴高 | **P1** |
| **成交历史** | 无/不完整 | CLOB + Dune | ❌ 缺失 | ❌ | ❌ | ❌ | ✅ | 🟡中 | P2 |
| **余额/钱包** | 硬编码 | CLOB API | ❌ 过期 | ❌ | ❌ | ✅ | ❌ | 🔴高 | P1 |
| **Gamma outcomePrices** | 仅 resolved 检查 | 不变 | ✅ 正确 | ❌ | ❌ | ❌ | ❌ | 🟢低 | — |

### 2. P0/P1/P2 数据修复项

#### P0 — 实盘前必须修

| # | 问题 | 影响 | 修复 |
|:-:|------|------|------|
| P0-1 | **结算方向用 Binance** (backtest + PnL) | PnL 可能算反，已有案例 | 改为 Chainlink RTDS |
| P0-2 | **没有 Chainlink RTDS 采集** | 无法验证 settlement | ✅ 已完成 (chainlink_collector + chainlink_btc_ticks) |

#### P1 — 正式测试前应该修

| # | 问题 | 影响 | 修复 |
|:-:|------|------|------|
| P1-1 | **midpoint 当成交价** | PnL 比实际好 | 改用 `best_ask` (买入) / `best_bid` (卖出) |
| P1-2 | **无 spread 过滤** | 98% spread 时也交易 | 从 `/book` 读取，spread > N% 禁止交易 |
| P1-3 | **无 depth 过滤** | 低深度推坏价格 | 从 `/book` 读取，depth < N 禁止交易 |
| P1-4 | **余额硬编码 $254** | R10 风险检查失效 | 从 CLOB API `GET /balance_allowance` 读取 |
| P1-5 | **Binance event time 未记录** | 无法和 Chainlink 对齐 | 加 `E` 字段存储 |

#### P2 — 后续优化

| # | 问题 | 修复 |
|:-:|------|------|
| P2-1 | CLOB `/book` timestamp 未使用 | 加数据新鲜度检查 |
| P2-2 | `/last-trade-price` 未使用 | 加成交价验证 |
| P2-3 | Gamma `endDate` 未使用 | 用于精确 settlement 时间 |
| P2-4 | chainlink `full_accuracy_value` 仅存 raw_payload | 可额外解析高精度值 |
| P2-5 | `resolvationSource` 未记录 | 可存 market 表 |

### 3. 当前绝对不能用于回测/实盘评估的数据

| 数据 | 原因 |
|------|------|
| **Binance 判 settlement** (btc_paper_backtest, honest_backtest, dune_backtest) | 与 Chainlink 官方结算不一致，边缘市场可能算反 |
| **midpoint 成交价模拟** | midpoint ≠ 可成交价，低流动性时差距 98% |
| **无 spread/depth 的 PnL** | 未扣除 spread 成本，PnL 被高估 |
| **硬编码余额** | 余额与实际不符时，R10 风险检查失效 |
| **Gamma outcomePrices 当 settlement** | 是条件代币价格，不是 oracle 价格 |

### 4. 当前已经可信的数据

| 数据 | 原因 |
|------|------|
| **Chainlink RTDS live settlement feed** | ✅ 官方 resolution source，已验证 raw payload |
| **Binance WS live signal** | ✅ 最及时的 BTC 价格，适合策略信号 |
| **Gamma market discovery** | ✅ slug 生成正确，clobTokenIds 映射正确 |
| **CLOB /midpoint** | ✅ 与 UI 一致的价格，适合信号（但不适合执行） |
| **CLOB /price (best bid/ask)** | ✅ 官方 endpoint，适合执行价格（但尚未使用） |
| **chainlink_btc_ticks 表** | ✅ 保存 raw_payload，格式规范 |
| **checkpoint 持久化** | ✅ 崩溃后恢复状态 |

### 5. 仍需外部验证的问题

| # | 问题 | 来源 |
|:-:|------|------|
| 1 | Chainlink Data Stream 更新频率是多少？每秒？ | `https://data.chain.link/streams/btc-usd` |
| 2 | Polymarket 结算时间点是 endDate 精确值还是 endDate + N秒？ | 需看官方文档 |
| 3 | CLOB `/book` 的 timestamp 是秒还是毫秒？ | 当前显示 13位（毫秒） |
| 4 | CLOB `/prices-history` endpoint 的 interval/fidelity 参数含义 | 需测试 |
| 5 | Dune Analytics Polymarket 数据集的更新延迟 | 已知数小时 |
| 6 | CLOB `/balance_allowance` 返回格式 | 需读取实际 payload |
| 7 | `py_clob_client_v2` 官方包签名验证 | pip 安全 |

### 6. 保存 raw_payload 的必要性

| 数据源 | 当前保存 raw_payload? | 建议 |
|--------|:--------------------:|------|
| Chainlink RTDS | ✅ `chainlink_btc_ticks.raw_payload` | 保留，已用于本次审计验证 |
| CLOB /midpoint | ❌ | 可选，payload 简单（只有 `{"mid":"0.685"}`） |
| CLOB /book | ❌ | **建议保存**，包含完整订单簿快照 |
| CLOB /price | ❌ | 可选 |
| CLOB /last-trade-price | ❌ | 可选 |
| Gamma API | ❌ | 可选 |
| Binance WS | ❌ | 建议至少保存 event time |

---

## 关键修复优先级总结

```
P0  Chainlink settlement  (backtest/PnL 方向错误)
    ↳ 已完成: collector + table + health check
    ↳ 待完成: 接入 paper trade settlement 判断

P1  Execution price: midpoint → best ask/bid
    Spread/depth 过滤
    余额从硬编码 → CLOB API
    Binance event time 记录

P2  CLOB timestamp / last-trade-price / Gamma endDate
    full_accuracy_value 解析
```

下一步推荐：先用 Chainlink 修正 settlement，再修复 execution price。
