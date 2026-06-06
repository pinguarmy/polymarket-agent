#!/usr/bin/env python3
"""Polymarket BTC 5min Predictability Analysis - Pure Data Approach"""

import sqlite3
from collections import defaultdict

DB_PATH = "data/btc5m.db"

def q(sql, params=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

print("=" * 70)
print("POLYMARKET BTC 5MIN PREDICTABILITY ANALYSIS")
print("=" * 70)

# ============================================================
# 1. 结算结果分布
# ============================================================
print("\n### 1. 结算结果分布 (Resolution Distribution) ###")

res = q("""
    SELECT resolution, COUNT(*) as cnt 
    FROM markets 
    WHERE slug LIKE '%btc-updown-5m%' AND resolution IS NOT NULL
    GROUP BY resolution
""")
total_resolved = sum(r['cnt'] for r in res)
print(f"总窗口数: {total_resolved}")
for r in res:
    pct = r['cnt'] / total_resolved * 100
    print(f"  {r['resolution']}: {r['cnt']} ({pct:.1f}%)")

# ============================================================
# 2. 获取每个窗口的开盘价 
# ============================================================
print("\n### 2. Up价格区间 vs 胜率分析 ###")

price_data = q("""
    SELECT 
        m.slug,
        m.resolution,
        m.open_time,
        m.close_time,
        (SELECT p.midpoint FROM polymarket_price_snapshots p 
         WHERE p.token_id = m.yes_token_id 
         ORDER BY p.timestamp ASC LIMIT 1) as first_up_price,
        (SELECT p.midpoint FROM polymarket_price_snapshots p 
         WHERE p.token_id = m.yes_token_id 
         ORDER BY p.timestamp DESC LIMIT 1) as last_up_price
    FROM markets m
    WHERE m.slug LIKE '%btc-updown-5m%'
      AND m.resolution IS NOT NULL
    ORDER BY m.open_time
""")

valid_price_data = [d for d in price_data if d['first_up_price'] is not None]
print(f"有价格数据的窗口数: {len(valid_price_data)}")

# 按Up价格区间分组
price_buckets = {
    '0.01-0.20': [],
    '0.20-0.40': [],
    '0.40-0.60': [],
    '0.60-0.80': [],
    '0.80-0.99': [],
}

for d in valid_price_data:
    p = d['first_up_price']
    res = d['resolution']
    if p < 0.20:
        price_buckets['0.01-0.20'].append(res)
    elif p < 0.40:
        price_buckets['0.20-0.40'].append(res)
    elif p < 0.60:
        price_buckets['0.40-0.60'].append(res)
    elif p < 0.80:
        price_buckets['0.60-0.80'].append(res)
    else:
        price_buckets['0.80-0.99'].append(res)

print("\n价格区间   | Up数 | Down数 | 买Up胜率 | 买Down胜率")
print("-" * 60)
for bucket, results in sorted(price_buckets.items()):
    if not results:
        continue
    up_cnt = sum(1 for r in results if r == 'Up')
    down_cnt = sum(1 for r in results if r == 'Down')
    total = len(results)
    up_win_rate = up_cnt / total * 100 if total > 0 else 0
    down_win_rate = down_cnt / total * 100 if total > 0 else 0
    print(f"{bucket:12s} | {up_cnt:5d} | {down_cnt:6d} | {up_win_rate:7.1f}% | {down_win_rate:7.1f}%")

# ============================================================
# 3. 不同阈值买入的期望收益
# ============================================================
print("\n### 3. 不同买入阈值(C)的期望收益 ###")

thresholds = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.90]
print("\n阈值C | 买Up数 | 买Down数 | 买Up净收益 | 买Down净收益 | 最佳方向")
print("-" * 75)

for c in thresholds:
    up_profits = []
    down_profits = []
    
    for d in valid_price_data:
        p = d['first_up_price']
        res = d['resolution']
        if p is None:
            continue
        
        if p < c:
            profit = (1/p - 1) if res == 'Up' else -1
            up_profits.append(profit)
        
        if p > (1 - c):
            profit = (1/(1-p) - 1) if res == 'Down' else -1
            down_profits.append(profit)
    
    up_total = sum(up_profits) if up_profits else 0
    down_total = sum(down_profits) if down_profits else 0
    up_n = len(up_profits)
    down_n = len(down_profits)
    
    best = "买Up" if up_total > down_total else "买Down" if down_total > up_total else "neutral"
    print(f" C={c:.2f} | {up_n:6d} | {down_n:7d} | {up_total:9.2f} | {down_total:11.2f} | {best}")

# ============================================================
# 4. 最佳入场点
# ============================================================
print("\n### 4. 最佳入场点分析 (按Up价格细分) ###")

price_fine_buckets = []
for i in range(5, 95, 5):
    bucket_range = (i/100, (i+5)/100)
    wins = []
    for d in valid_price_data:
        p = d['first_up_price']
        res = d['resolution']
        if p is None:
            continue
        if bucket_range[0] <= p < bucket_range[1]:
            profit = (1/p - 1) if res == 'Up' else -1
            wins.append(profit)
    
    if wins:
        total_p = sum(wins)
        avg_p = total_p / len(wins)
        up_rate = sum(1 for w in wins if w > 0) / len(wins) * 100
        price_fine_buckets.append({
            'range': f"{bucket_range[0]:.2f}-{bucket_range[1]:.2f}",
            'n': len(wins),
            'total_profit': total_p,
            'avg_profit': avg_p,
            'up_rate': up_rate
        })

if price_fine_buckets:
    print("\nUp价格区间  | 样本数 | 总收益 | 平均收益 | Up胜率")
    print("-" * 60)
    sorted_buckets = sorted(price_fine_buckets, key=lambda x: x['avg_profit'], reverse=True)
    for b in sorted_buckets:
        print(f"{b['range']:12s} | {b['n']:6d} | {b['total_profit']:7.2f} | {b['avg_profit']:8.3f} | {b['up_rate']:.1f}%")
    
    print(f"\n最佳区间: {sorted_buckets[0]['range']} 平均收益 {sorted_buckets[0]['avg_profit']:.3f}")
    print(f"最差区间: {sorted_buckets[-1]['range']} 平均收益 {sorted_buckets[-1]['avg_profit']:.3f}")

# ============================================================
# 5. BTC波动幅度 vs 胜率
# ============================================================
print("\n### 5. BTC波动幅度 vs 胜率分析 ###")

# 用简单SQL获取所有窗口的开闭价格
windows = q("""
    SELECT 
        m.slug,
        m.resolution,
        m.open_time,
        m.close_time,
        (SELECT b.price FROM binance_btc_ticks b WHERE b.timestamp >= m.open_time ORDER BY b.timestamp ASC LIMIT 1) as open_price,
        (SELECT b.price FROM binance_btc_ticks b WHERE b.timestamp >= m.close_time ORDER BY b.timestamp ASC LIMIT 1) as close_price
    FROM markets m
    WHERE m.slug LIKE '%btc-updown-5m%' AND m.resolution IS NOT NULL
""")

vol_data = []
for w in windows:
    if w['open_price'] and w['close_price'] and w['open_price'] > 0:
        pct = abs(w['close_price'] - w['open_price']) / w['open_price'] * 100
        vol_data.append({
            'slug': w['slug'],
            'resolution': w['resolution'],
            'open_price': w['open_price'],
            'close_price': w['close_price'],
            'change_pct': pct
        })

print(f"成功匹配波动数据的窗口: {len(vol_data)}")

# 波动统计
vols = [v['change_pct'] for v in vol_data]
print(f"波动范围: {min(vols):.3f}% - {max(vols):.3f}%")
print(f"平均波动: {sum(vols)/len(vols):.3f}%")

# 按波动区间分组
vol_buckets = {
    '0-0.10%': [],
    '0.10-0.20%': [],
    '0.20-0.50%': [],
    '0.50-1.00%': [],
    '>1.00%': []
}

for v in vol_data:
    c = v['change_pct']
    res = v['resolution']
    if c < 0.10:
        vol_buckets['0-0.10%'].append(res)
    elif c < 0.20:
        vol_buckets['0.10-0.20%'].append(res)
    elif c < 0.50:
        vol_buckets['0.20-0.50%'].append(res)
    elif c < 1.00:
        vol_buckets['0.50-1.00%'].append(res)
    else:
        vol_buckets['>1.00%'].append(res)

print("\n波动区间    | 总数 | Up数 | Down数 | 买Up胜率 | 买Down胜率")
print("-" * 70)
for bucket in ['0-0.10%', '0.10-0.20%', '0.20-0.50%', '0.50-1.00%', '>1.00%']:
    results = vol_buckets.get(bucket, [])
    if not results:
        continue
    up_cnt = sum(1 for r in results if r == 'Up')
    down_cnt = sum(1 for r in results if r == 'Down')
    total = len(results)
    up_win_rate = up_cnt / total * 100
    down_win_rate = down_cnt / total * 100
    print(f"{bucket:12s} | {total:4d} | {up_cnt:4d} | {down_cnt:5d} | {up_win_rate:8.1f}% | {down_win_rate:10.1f}%")

# ============================================================
# 6. 自相关检验
# ============================================================
print("\n### 6. 相邻窗口结果自相关分析 ###")

ordered_res = [w['resolution'] for w in windows]

same_count = sum(1 for i in range(1, len(ordered_res)) if ordered_res[i] == ordered_res[i-1])
diff_count = len(ordered_res) - 1 - same_count

print(f"相邻窗口结果一致: {same_count}")
print(f"相邻窗口结果不同: {diff_count}")
if same_count + diff_count > 0:
    consistency = same_count / (same_count + diff_count) * 100
    print(f"一致性比率: {consistency:.1f}%")
    print(f"期望如果随机: 50.0%")

# ============================================================
# 7. Run Test
# ============================================================
print("\n### 7. 连续相同结果分析 (Run Test) ###")

runs = []
current_run = 1
for i in range(1, len(ordered_res)):
    if ordered_res[i] == ordered_res[i-1]:
        current_run += 1
    else:
        runs.append(current_run)
        current_run = 1
runs.append(current_run)

run_lengths = defaultdict(int)
for r in runs:
    run_lengths['5+'] += 1 if r >= 5 else 0
    run_lengths[r] += 1 if r < 5 else 0

print("连续相同结果分布:")
for k in sorted(run_lengths.keys(), key=lambda x: (isinstance(x, str), x)):
    print(f"  {k}次连续: {run_lengths[k]}次")

# ============================================================
# 8. 综合结论
# ============================================================
print("\n" + "=" * 70)
print("### 综合分析结论 ###")
print("=" * 70)

up_total = sum(1 for r in ordered_res if r == 'Up')
down_total = sum(1 for r in ordered_res if r == 'Down')
total = len(ordered_res)

print(f"\n1. 基础分布:")
print(f"   总窗口数: {total}")
print(f"   Up结算: {up_total} ({up_total/total*100:.1f}%)")
print(f"   Down结算: {down_total} ({down_total/total*100:.1f}%)")
print(f"   → 基础概率约 50/50，与随机一致")

print(f"\n2. 相邻窗口一致性:")
print(f"   一致: {same_count} ({same_count/(same_count+diff_count)*100:.1f}%)")
print(f"   不同: {diff_count} ({diff_count/(same_count+diff_count)*100:.1f}%)")
print(f"   → {'无显著自相关' if abs(consistency-50) < 5 else '存在一定自相关'}")

print(f"\n3. BTC波动 vs 结果:")
for bucket in ['0-0.10%', '0.10-0.20%', '0.20-0.50%', '0.50-1.00%', '>1.00%']:
    results = vol_buckets.get(bucket, [])
    if results:
        up_r = sum(1 for r in results if r == 'Up') / len(results) * 100
        print(f"   {bucket}: Up胜率 {up_r:.1f}% (n={len(results)})")

print(f"\n4. 最终判断:")
print(f"   从纯数据角度，Polymarket BTC 5min市场：")
print(f"   - 结果分布接近50/50随机分布")
if price_fine_buckets:
    best = sorted(price_fine_buckets, key=lambda x: x['avg_profit'], reverse=True)[0]
    worst = sorted(price_fine_buckets, key=lambda x: x['avg_profit'])[0]
    print(f"   - 最佳买入区间 {best['range']} 平均收益 {best['avg_profit']:.3f}")
    print(f"   - 最差买入区间 {worst['range']} 平均收益 {worst['avg_profit']:.3f}")
    print(f"   → 结论：该市场接近有效随机市场，难以预测")
