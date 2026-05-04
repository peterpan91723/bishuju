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
yesterday_data, funding_data, rsi70_data, rsi60_data = fetch_daily_data(symbols)
rsi_data = fetch_weekly_data(symbols)
monthly_rsi_data = fetch_monthly_data(symbols)
output = build_rankings(symbols, yesterday_data, funding_data, rsi_data, monthly_rsi_data, rsi70_data, rsi60_data)
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
- `fetch_daily_data(symbols)` → `(yesterday_data, funding_data, rsi70_data, rsi60_data)` — 每次都抓
- `fetch_weekly_data(symbols, force=False)` → `rsi_data` — **带缓存**
- `fetch_monthly_data(symbols, force=False)` → `monthly_rsi_data` — **带缓存**（仍用于 `monthlyClosedVolume`）
- `get_daily_indicators(symbols)` — 一次抓 100 根日 K，同时算 RSI70 + RSI60 两组结果
- `build_rankings(...)` — 组装 7 个排行榜数据
- `save_data(output)` — 写入 `data/rankings.json`

### 周/月数据缓存

周/月线 K 线只在周一 / 月初 UTC 00:00 切换，每小时重抓是浪费。`fetch_weekly_data` 和 `fetch_monthly_data` 自动带缓存：

- `data/cache_weekly.json` / `data/cache_monthly.json` — 含 `fetchedAt` ISO 时间戳和原始数据
- 启动时若缓存的 `fetchedAt >= 最近一次 K 线收盘时间`（周线=最近一个周一 00:00 UTC；月线=本月 1 号 00:00 UTC），直接复用，**不调用 API**
- 否则抓新数据并刷新缓存
- 传 `force=True` 可绕过缓存
- 缓存文件被 workflow 一并 commit，跨 runner 持久化

效果：周线 API 抓取从每小时 1 次 → 每周 1 次（减少 ~96%）；月线从每小时 1 次 → 每月 1 次（减少 ~99.9%）。

### 7 个排行榜 Tab

| Tab key | 数据来源 | 排序 |
|---|---|---|
| `yesterdayChange` | 日K收盘涨跌幅 | 按涨幅 |
| `weeklyClosedVolume` | 最新已收盘周K线 USDT 成交额 | 按成交额，取 TOP 50 |
| `monthlyClosedVolume` | 最新已收盘月K线 USDT 成交额 | 按成交额，取 TOP 50 |
| `fundingRate` | 实时资金费率 | 默认升序 |
| `weeklyRsi` | 周线RSI(14)，仅显示递增 | 按昨日USDT成交额 |
| `dailyRsi70` | 日线 RSI≥70 + EMA9>21>55 + 百分比间距 (EMA9-21)/EMA21、(EMA21-55)/EMA55 同时较上一根扩大 + 当日成交额 > SMA(20) | 按当日USDT成交额 |
| `dailyRsi60` | 同 `dailyRsi70`，阈值 RSI≥60 | 按当日USDT成交额 |

`dailyRsi70` 与 `dailyRsi60` 共用一次日 K 抓取（`get_daily_indicators`），RSI70 是 RSI60 的子集。

`weeklyClosedVolume` / `monthlyClosedVolume` 复用 `get_weekly_rsi` / `get_monthly_rsi` 已抓取的周/月 K 线，无额外 API 调用；`closedVolume` 字段取自 `klines[:-1][-1][7]`（最新已收盘 K 线的 quote_asset_volume）。

### RSI 计算

使用 Wilder's Smoothing（与 TradingView 一致）。周线/月线 K 线末根为未收盘K线，计算时排除（`klines[:-1]`）。`calc_rsi_last_two()` 返回倒数第二根和最新收盘 RSI，用于判断递增。

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

`script.js` 中 `TABS_CONFIG` 对象定义每个 tab 的表头文字、数值格式化函数和副标题。新增 tab 时需同步修改：`TABS_CONFIG`（script.js）、`fetch_data.py` 的 `build_rankings()` 输出、`index.html` 的 tab 按钮。

TradingView 导出格式：`BINANCE:SYMBOLUSDT.P`（永续合约后缀 `.P`）。
