# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

币安USDT永续合约排行榜网站，部署于 GitHub Pages：https://peterquant1.github.io/bishuju/

数据通过 GitHub Actions 每小时自动抓取更新，静态 JSON 文件托管在仓库中供前端读取。

## Environment

**必须使用虚拟环境**，系统 Python 上有实盘量化程序在跑：

```bash
# 激活虚拟环境
venv\Scripts\activate       # Windows
source venv/bin/activate    # Linux/Mac

# 安装依赖
pip install requests pysocks
```

## Running the Data Fetcher

```bash
# 单次全量抓取（测试用）
python -c "
from fetch_data import *
symbols = get_usdt_perpetual_symbols()
yesterday_data, funding_data, rsi70_data, rsi59_data, sar_flip_data = fetch_daily_data(symbols)
rsi_data = fetch_weekly_data(symbols)
monthly_rsi_data = fetch_monthly_data(symbols)
output = build_rankings(symbols, yesterday_data, funding_data, rsi_data, monthly_rsi_data, rsi70_data, rsi59_data, sar_flip_data)
save_data(output)
print(output['updateTime'])
"

# 持续运行模式（本地调试用，生产不需要，GitHub Actions 负责更新）
python fetch_data.py
```

本地运行时 Binance 可直接访问（无需代理）。GitHub Actions 服务器在美国，需通过 VLESS 代理访问。

## Architecture

### Data Flow

```
GitHub Actions (每小时) → fetch_data.py → data/rankings.json → GitHub Pages → index.html + script.js
```

前端每 30 秒自动 fetch `data/rankings.json`，无后端服务器。

### fetch_data.py 结构

- `get_usdt_perpetual_symbols()` — 获取全部 USDT 永续合约列表
- `batch_fetch_klines(symbols, params)` — 分批并发请求 K 线（50/批，10 并发，批间 0.5s 延迟，防限频）
- `fetch_daily_data(symbols)` → `(yesterday_data, funding_data, rsi70_data, rsi59_data, sar_flip_data)` — 每次都抓
- `fetch_weekly_data(symbols, force=False)` → `rsi_data` — **带缓存**
- `fetch_monthly_data(symbols, force=False)` → `monthly_rsi_data` — **带缓存**（仍用于 `monthlyClosedVolume`）
- `get_daily_indicators(symbols)` — 一次抓 499 根日 K，同时算 RSI70 + RSI59 + SAR 翻多三组结果
- `build_rankings(...)` — 组装 8 个排行榜数据
- `save_data(output)` — 写入 `data/rankings.json`

### 周/月数据缓存

周/月线 K 线只在周一 / 月初 UTC 00:00 切换，每小时重抓是浪费。`fetch_weekly_data` 和 `fetch_monthly_data` 自动带缓存：

- `data/cache_weekly.json` / `data/cache_monthly.json` — 含 `fetchedAt` ISO 时间戳和原始数据
- 启动时若缓存的 `fetchedAt >= 最近一次 K 线收盘时间`（周线=最近一个周一 00:00 UTC；月线=本月 1 号 00:00 UTC），直接复用，**不调用 API**
- 否则抓新数据并刷新缓存
- 传 `force=True` 可绕过缓存
- 缓存文件被 workflow 一并 commit，跨 runner 持久化

效果：周线 API 抓取从每小时 1 次 → 每周 1 次（减少 ~96%）；月线从每小时 1 次 → 每月 1 次（减少 ~99.9%）。

### 8 个排行榜 Tab

| Tab key | 数据来源 | 排序 |
|---|---|---|
| `yesterdayChange` | 日K收盘涨跌幅 | 按涨幅 |
| `weeklyClosedVolume` | 最新已收盘周K线 USDT 成交额 | 按成交额，取 TOP 50 |
| `monthlyClosedVolume` | 最新已收盘月K线 USDT 成交额 | 按成交额，取 TOP 50 |
| `fundingRate` | 实时资金费率 | 默认升序 |
| `weeklyRsi` | 周线RSI(14)，仅显示递增 | 按昨日USDT成交额 |
| `dailyRsi70` | 日线 RSI≥70 + EMA9>21>55 + 百分比间距 (EMA9-21)/EMA21、(EMA21-55)/EMA55 同时较上一根扩大 + 当日**币本位**成交量 > SMA(20) + Parabolic SAR 多头 + CVD 当根 > 上一根 | 按当日USDT成交额 |
| `dailyRsi59` | 同 `dailyRsi70`，阈值 RSI≥59 | 按当日USDT成交额 |
| `dailySar` | 日线 Parabolic SAR 翻多**首根**（当根多头 + 上一根空头）+ 当日**币本位**成交量 > SMA(20) | 按当日USDT成交额 |

> **量能确认细节**：过滤判断用币本位成交量 (`k[5]`，与 TradingView 默认 volume 指标一致)，列表排序用 USDT 成交额 (`k[7]`)。两者口径不同，前者用于"对齐 TV 信号"，后者用于"按金额排名"。
>
> **SAR 细节**：`calc_sar(highs, lows, closes, 0.02, 0.02, 0.2)` 严格对齐 Pine v5 `ta.sar(0.02, 0.02, 0.2)`：bar 1 用 close[1] vs close[0] 决定初始趋势；每根递推 `SAR = SAR_prev + AF*(EP-SAR_prev)`；多头时 SAR ≤ min(low[i-1], low[i-2])（空头镜像）；反转时 SAR=前段 EP 再 max/min 至当根+前根 high/low；创新极值时更新 EP，AF 累加 inc 上限 max_af。多头 ⇔ close > sar。
>
> **CVD 细节**：`calc_cvd_last_two(klines, 14)` 与 Pine "Cumulative Volume Delta" by Ankit_1618 对齐。每根 K 线按形状拆分 buying/selling volume：阳线 body 归买盘、影线一半各归买卖；阴线 body 归卖盘、影线一半各归买卖；平盘全归影线（买卖各半）。两条序列分别 EMA(14) 平滑（first-bar init），CVD = buying_ema − selling_ema。volume 用 k[5] 币本位。

`dailyRsi70` / `dailyRsi59` / `dailySar` 共用一次日 K 抓取（`get_daily_indicators`），RSI70 是 RSI59 的子集；SAR 翻多与 RSI 系列**逻辑独立**（dailySar 的币既可能不在 RSI59 中，也可能在）。

`weeklyClosedVolume` / `monthlyClosedVolume` 复用 `get_weekly_rsi` / `get_monthly_rsi` 已抓取的周/月 K 线，无额外 API 调用；`closedVolume` 字段取自 `klines[:-1][-1][7]`（最新已收盘 K 线的 quote_asset_volume）。

### RSI 计算

使用 Wilder's Smoothing（与 TradingView ta.rsi 对齐）。周线/月线 K 线末根为未收盘K线，计算时排除（`klines[:-1]`）。`calc_rsi_last_two()` 单遍 Wilder 递推同时返回倒数第二根和最新收盘 RSI，用于判断递增。

**精度要求**：`calc_rsi` 返回**未经四舍五入的 float**，阈值比较（如 `< 59`、`>= 70`）必须用全精度，否则 58.996 会错误地舍入为 59.00 通过过滤。展示侧统一用 `.toFixed(2)`。

### EMA 计算

`calc_ema` 使用 first-bar 初始化（与 TradingView Pine `ta.ema` 内部递推方式一致），`ema = closes[0]`，逐根递推 `ema = α·price + (1-α)·ema`，α=2/(period+1)。

**暖机长度**：日线 K 线 `limit=499`，使 EMA55 有 ≥444 根递归，初始残差 ≈ 1e-8（机器精度内）与 TV 长图对齐。

**Binance fapi /klines 权重分桶**：`[1,100)→1`、`[100,500)→2`、`[500,1000]→5`、`>1000→10`。`limit=499` 仍属 weight=2 区间，IP 权重与 `limit=100` 完全一致，**无任何限流增加风险**。如需进一步增大暖机（暖机至 ≥999 根），需接受 weight=5 即每次抓取 IP 权重升 2.5x，每分钟限额下仍有充足余量。

### 中文合约名处理

Binance 有 3 个中文名合约，`SYMBOL_RENAME` 字典将其映射为英文展示名，在 `build_rankings()` 中通过 `rename_symbol()` 统一处理。

### GitHub Actions 代理

Binance 对美国 IP 返回 HTTP 451，需通过 VLESS 代理：

- Secret 名称：`PROXY_URL`，值为 VLESS URL（`vless://...` 格式）
- Workflow 中自动下载 Xray-core，解析 VLESS URL 生成配置，启动本地 `socks5://127.0.0.1:10808`
- Python 步骤通过 `ALL_PROXY` / `HTTPS_PROXY` 环境变量使用该代理
- **代理到期后只需更新 GitHub Secret 的值**，workflow 文件无需修改

更新 Secret：`gh secret set PROXY_URL --repo peterquant1/bishuju --body "vless://..."`

手动触发更新：`gh workflow run update-data.yml --repo peterquant1/bishuju`

### 前端

`script.js` 中 `TABS_CONFIG` 对象定义每个 tab 的表头文字、数值格式化函数和副标题。

TradingView 导出格式：`BINANCE:SYMBOLUSDT.P`（永续合约后缀 `.P`）。

### 增删/重命名 Tab 的修改清单

新增、删除或重命名 tab 时，**所有以下位置必须同步**（漏改会导致 workflow 报错或前端拿不到数据）：

1. `fetch_data.py`：
   - `get_daily_indicators()` 内部 dict 名（如 `rsi70_data` / `rsi59_data`）
   - 函数返回签名 + docstring
   - `fetch_daily_data()` 解构和返回签名
   - `build_rankings()` 函数签名
   - `build_rankings()` 内部新构建的 list 变量名
   - `build_rankings()` 返回 dict 的 key（`"dailyRsi70"` / `"dailyRsi59"` 等）
   - `main()` 内 tuple 解构 + `build_rankings()` 调用
   - `main()` 循环模式内的 tuple 解构 + `build_rankings()` 调用
2. `.github/workflows/update-data.yml`：inline Python 中的解构和 `build_rankings()` 调用
3. `index.html`：`<button class="tab" data-tab="...">` 标签 + bump `script.js?v=N` 缓存版本
4. `script.js`：
   - `TABS_CONFIG` 中对应 key 和 subFormat
   - `getColorClass()` 中需要 neutral 颜色的 tab 列表
5. `CLAUDE.md`：本表格 + 数据流注释

完成后用 `Grep` 搜旧名字（如 `rsi60`/`RSI60`）确认无残留。

### 精度对齐 TradingView 的关键细节

所有日线指标（RSI、EMA、SAR、CVD）都做了 bit 级别对齐：

- **RSI**：calc_rsi 全精度返回，**绝不**在阈值比较前 round
- **EMA**：first-bar 初始化（不是 SMA-init），与 Pine `ta.ema` 内部递推一致
- **SAR**：bar 1 初始化、反转 EP+边界、AF 累加，全部按 Pine v5 `ta.sar` 语义
- **CVD**：按 K 线形状拆分 buying/selling volume → EMA(14) 平滑 → 做差，与 Pine "Cumulative Volume Delta" by Ankit_1618 对齐
- **暖机**：日 K `limit=499`，EMA55 残差 ~1e-8，远低于 TV 显示精度；周/月 K `limit=100`（短周期 RSI(14) 收敛足够）
- **量能**：过滤判断用 k[5] 币本位（与 TV volume 指标一致）；列表排序用 k[7] USDT 成交额

修改任何指标公式前，先在 TV 上找一根已知 K 线对照确认 1-2 位小数一致再 commit。
