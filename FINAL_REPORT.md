# Polymarket BTC 5min — 三方合并最终报告

> 来源：DeepSeek/Mimo（13,000+ 组合回测） + Codex CLI（代码扫描） + Claude（实施计划）

---

## 第1章：策略现状

### 最优参数（三方一致）

```
BTC YES = $3     BTC NO  = $5        ← Codex: "不对称是最优的"
YES_LOW = 0.25   YES_HIGH = 0.45     ← Claude: "最优值从1,225组合回测确认"
SL YES  = 4×     SL NO   = 3×        ← Mimo: "SL不对称通过交叉验证"
TP YES  = 0.85   TP NO   = 0.88      ← DeepSeek: "TP不对称多赚$18"
MAX_ENTRIES = 1                       ← 三方一致确认不加仓
SCALE_SIZE = $10 (可调)
```

### 回测结果（213 窗口，$10/笔）

```
PnL: +$120.39 | 胜率: 63.2% | 87笔交易

退出方式:
  TP:      45笔  +$172.54  ← 核心利润(Claude: "退出质量比入场更重要")
  SL:      19笔   -$33.30
  Settlement: 27笔  -$18.85

TP真实性: 96%结算时也是赢的 → 不是假跳
交叉验证: 全部通过
```

### 架构现状（Claude 完整描述）

```
Binance WS (BTC price) ─┐
                          ├──► realtime_trader.py ──► CLOB V2 (orders)
Polymarket CLOB (YES) ───┘         │
                                    ├──► btc5m.db (SQLite)
                                    ├──► logs/paper_trades.jsonl
                                    └──► logs/trade_alert.json
```

**阶段成熟度（Claude）:**
- ✅ Phase 1–4: 市场数据、纸交易、信号、订单草稿 — **完成**
- ✅ Phase 5 脚手架: `live_trader.py`, `clob_client.py` — **完成**
- 🔴 Phase 5 执行: TP/SL 不提交 CLOB 卖单 — **未完成**
- 🔴 Phase 5 填充追踪: 订单未验证填充 — **未完成**

---

## 第2章：代码问题 — 三方汇总

### 🔴 实盘阻塞（三方一致 — 必须先修才能实盘）

| # | Bug | 发现者 | 严重度 |
|---|-----|--------|--------|
| 1 | TP/SL 只写日志，不提交 CLOB 卖单 | Codex + Claude | 🔴 |
| 2 | 下单返回 success 即记为成交，不查实际 fill | Codex + Claude | 🔴 |
| 3 | `realtime_trader.py` 参数跟回测最优不一致 | Claude | 🔴 |
| 4 | 连续失败计数器未实现，kill-switch 自动触发不生效 | Claude | 🔴 |

**Bug 1 详解 — 三方都认为最严重：**
`_close_position()` 只写入 `paper_trades.jsonl` 和更新内存 PnL，**从不提交 CLOB 取消/卖单**。实盘模式下仓位永远不会真正关闭。

**Bug 3 详解：**
当前 live 阈值 (`YES_LOW=0.50`, `YES_HIGH=0.55`, `MIN_BTC_MOVE=7.0`) 与回测最优 (`YES_LOW=0.25`, `YES_HIGH=0.45`, BTC 不对称) 完全不同。意味着**你从未实际测试过被验证的策略。**

### 🟡 中优先级（需在放大前处理）

| # | Issue | 发现者 |
|---|-------|--------|
| 5 | 回测用 midpoint 而非 bid/ask，低估滑点 | Codex + Claude |
| 6 | 无交易成本模型（Polymarket ~0.02/边） | Codex + Claude |
| 7 | 87 笔交易，0.02 成本 = $17.40 = 14% 的 +$120 PnL | Claude |
| 8 | `honest_backtest.py` 硬编码绝对路径 | Claude |
| 9 | 只有 2 个测试文件覆盖 <5% 代码库 | Claude |
| 10 | WebSocket 重连竞争条件 | Codex（已修） |
| 11 | `except:pass` 静默失败 | Codex（已修） |
| 12 | DB 连接泄漏 | Codex（已修） |
| 13 | 价格字段不一致（limit vs fill） | Codex（已修） |

### ⚪ 已修复（三方确认）

- --live 双门禁 ✅
- checkpoint token_ids 恢复 ✅
- WebSocket 重连锁 ✅
- except:pass → logger.warning ✅
- DB 连接泄漏 ✅
- 价格字段不一致 ✅

---

## 第3章：策略评估 — 三方观点

### DeepSeek/Mimo（回测驱动）

```
Edge 存在但很薄。本质不是"跟随操纵者"，
而是均值回归 + 流动性溢价。
213窗口 +$120 = 每窗口 $0.57，不足以确认。
需要 1,000+ 窗口重新验证。
```

### Codex（代码驱动）

```
213 窗口统计显著性偏低。
建议 walk-forward 验证 + 交易成本压力测试。
分批止盈优于单一 TP 线。
当前参数可小仓观察，不建议放大。
```

### Claude（架构驱动）

```
核心假设：薄盘口市场中鲸鱼操纵引起价格偏离。
回测有 63.2% 胜率 + 交叉验证通过，理论成立。
但回测未考虑滑点/成本/成交失败，真实 edge 可能更小。
至少 1,000 窗口（10天数据）后才可确认。
```

### 三方共同结论

| 话题 | 三方同意？ |
|------|-----------|
| 策略有微弱 edge | ✅ |
| 203 窗口不够 | ✅ 都建议 1,000+ |
| 必须先加交易成本回测 | ✅ |
| 不加仓（MAX=1） | ✅ |
| YES/NO 必须分离 | ✅ |
| TP 是核心利润来源 | ✅ |
| 当前参数可小仓试 | ⚠️ Codex 谨慎，DeepSeek 中立，Claude 有条件同意 |

---

## 第4章：改进建议 — 完整列表

### DeepSeek/Mimo 提出的

1. **Size 放大** — 线性放大，$50/笔 → +$600
2. **多窗口同时交易** — 5 分钟窗口彼此独立
3. **扩展到 ETH/SOL/15min** — 相同策略逻辑
4. **TP 不对称 0.85/0.88** — 我们跑了 1,225 组合确认

### Codex 提出的

1. **分批止盈** — TP1=0.70 卖一半，TP2=0.85 卖剩余
2. **时间止盈** — 最后 60-90 秒价格 >0.70 就退
3. **动态止损** — trailing stop，从最高点回撤一定比例
4. **最后 60 秒禁止入场** — 比当前 15 秒保护更严格
5. **独立冷却** — YES/NO 独立冷却时间
6. **动态仓位** — A 级信号全仓，B 级半仓，C 级不交易
7. **连亏降仓** — 连续亏损自动缩小仓位
8. **组合策略** — FOLLOW + Momentum + Mean Reversion
9. **多时段建模** — 亚/欧/美盘分别调参数
10. **交易成本压力测试** — spread+0.02, 10-30% 成交失败, 1-3 秒延迟

### Claude 提出的

1. **分批止盈** — TP1=0.70 卖 50%，TP2=0.85/0.88 卖剩余（与 Codex 一致）
2. **时间止损** — t=180s 未盈利 0.02 就退
3. **独立 YES/NO cooldown** — `entries_yes` 和 `entries_no` 分开
4. **Trailing stop** — +0.04 后止损移到 break-even
5. **Walk-forward 验证** — 4+ fold rolling 验证
6. **多资产扩展** — ETH/SOL 5min，BTC correlated
7. **Dashboard 实时连接** — WebSocket 到 trader
8. **单元测试** — risk_engine, signals, backtest

---

## 第5章：约束项（三方一致认为不可协商）

| 约束 | 来源 | 原因 |
|------|------|------|
| 实盘前必须先修 Bug 1+2 | 三方 | 仓位永不关闭 |
| 实盘前参数必须跟回测一致 | Claude | 否则测试的是不同策略 |
| 数据收集 ≥10 天/1,000 窗口 | 三方 | 2 天统计不足 |
| 参数修改后必须 --dry-run ≥5 次交易 | Claude | 防止意外行为 |
| MAX_ORDER_SIZE ≤$25 直到 walk-forward 通过 | Claude | Size 是所有错误乘数 |
| Kill-switch 必须自动触发 | Claude | 防连续失败 |
| 永远不追单（取消即跳过） | Claude | 防滑点失控 |

---

## 第6章：实施路线图（Claude 结构 + 三方内容）

### Phase A：实盘前必修（阻塞项）

```
A1: 参数同步
    把 realtime_trader.py 参数改为 STRATEGY_REPORT 最优值
    文件: src/realtime_trader.py 行 70-98
    风险: 低（dry-run 模式）

A2: CLOB 卖单
    在 _close_position() 中实盘时提交 CLOB SELL
    文件: src/realtime_trader.py
    风险: 中（需要 CLOB auth）

A3: Fill 验证
    下单后轮询 get_order() 直到 MATCHED
    文件: src/realtime_trader.py
    风险: 低

A4: 连续失败计数器 + kill-switch
    3 次连续失败自动触发
    文件: src/realtime_trader.py
    风险: 低

A5: 修复硬编码路径
    honest_backtest.py 改为 Path(__file__)
    文件: src/honest_backtest.py
    风险: 无
```

### Phase B：回测完整性（放大前必做）

```
B1: 加入 bid/ask 滑点模型（slippage=0.005）
B2: 加入交易成本（0.02/边）
B3: Walk-forward 验证脚本（4+ fold）
B4: 收集 10+ 天数据（等待态）
```

### Phase C：策略改进（可并行）

```
C1: 分批止盈（TP1=0.70 卖一半，TP2=0.85 卖剩余）
    - DeepSeek: 没测过，值得试
    - Codex: 强烈推荐
    - Claude: 同推荐

C2: 时间止损（t=180s 未盈利 0.02 退）
    - Codex + Claude 独立提出

C3: 独立 YES/NO cooldown
    - 由 Claude 提出，Codex 赞同

C4: Trailing stop（+0.04 后保本）
    - 三方都认为值得试
```

### Phase D：基础设施

```
D1: risk_engine 单元测试
D2: signal 检测单元测试
D3: 回测引擎单元测试
D4: Dashboard WebSocket 实时连接
```

---

## 第7章：推荐执行顺序

```
今天:       A1（参数同步）+ A5（路径修复）
这周:       A2 + A3 + A4（实盘阻塞项）
下周:       B1 + B2（真实回测）→ 重跑全部回测
持续:       收集数据，等 1,000+ 窗口
通过后:     B3（walk-forward）→ 如果通过，小仓实盘（$5/笔）
然后:       C1-C4（在实盘数据上改进）
```

---

## 第8章：未解决问题

1. **目标资金？** $10/笔 还是 $8-15？取决于你的风险承受
2. **CLOB 卖单机制？** 取消原买单（如果仍开放）还是提交新卖单？5 分钟窗口中买单一大概率已成交 → **需要新卖单**
3. **Fill 轮询超时？** 5 秒未成交 → 取消并跳过（无追单规则）
4. **A1-A4 后 dry-run 天数？** 推荐 ≥5 次交易 >=2 次信号触发
