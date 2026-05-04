import os
import requests
import json
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://fapi.binance.com"
TOP_N = 50
REQUEST_TIMEOUT = 10  # 请求超时（秒）

# 中文合约名映射为英文
SYMBOL_RENAME = {
    "币安人生USDT": "BIANRENSHENGUSDT",
    "我踏马来了USDT": "WOTAMALAILIAOUSDT",
    "龙虾USDT": "LONGXIAUSDT",
}


def _api_get(url, params=None):
    """统一的API请求，带超时和状态码检查"""
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def rename_symbol(symbol):
    """中文合约名转英文"""
    return SYMBOL_RENAME.get(symbol, symbol)


def get_usdt_perpetual_symbols():
    """获取所有USDT永续合约交易对"""
    data = _api_get(f"{BASE_URL}/fapi/v1/exchangeInfo")
    return [
        s["symbol"]
        for s in data["symbols"]
        if s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
        and s["symbol"] != "USDCUSDT"
    ]



MAX_WORKERS = 10  # 并发数，降低避免触发限频
BATCH_DELAY = 0.5  # 每批之间延迟（秒）


def _fetch_kline(symbol, params):
    """单个合约K线请求"""
    data = _api_get(f"{BASE_URL}/fapi/v1/klines", params={"symbol": symbol, **params})
    return symbol, data


def batch_fetch_klines(symbols, params):
    """分批并发请求K线，避免触发限频"""
    results = {}
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_kline, s, params): s for s in batch}
            for future in as_completed(futures):
                try:
                    symbol, klines = future.result()
                    results[symbol] = klines
                except Exception as e:
                    symbol = futures[future]
                    print(f"  [警告] {symbol} K线请求失败: {e}")
                    continue
        if i + batch_size < len(symbols):
            time.sleep(BATCH_DELAY)
    return results


def get_yesterday_change(symbols):
    """通过日K线计算昨日涨幅（分批并发）"""
    now = datetime.now(timezone.utc)
    end_time = int(
        now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    start_time = end_time - 2 * 86400 * 1000
    params = {"interval": "1d", "startTime": start_time, "endTime": end_time, "limit": 2}

    all_klines = batch_fetch_klines(symbols, params)
    results = {}
    for symbol, klines in all_klines.items():
        if len(klines) >= 1:
            k = klines[-1]
            open_price = float(k[1])
            close_price = float(k[4])
            volume_usdt = float(k[7])
            if open_price > 0:
                change_pct = (close_price - open_price) / open_price * 100
                results[symbol] = {
                    "changePercent": round(change_pct, 2),
                    "volume": round(volume_usdt, 2),
                    "open": open_price,
                    "close": close_price,
                }

    return results


def calc_rsi(closes, period=14):
    """计算 RSI（Wilder's smoothing，与 TradingView ta.rsi 对齐）。

    返回未经 round 的 float —— 阈值比较必须用全精度，否则 59.996 会被
    错误地舍入到 60.00 通过过滤。展示侧请自行 round/toFixed。
    """
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def calc_rsi_last_two(closes, period=14):
    """单遍计算 RSI 序列的 (prev, curr)，与 calc_rsi 数学等价（性能 ~2x）。

    避免分别对 closes[:-1] 和 closes 各跑一次 Wilder 递推。
    """
    if len(closes) < period + 2:
        return None, None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    n_deltas = len(deltas)

    # Wilder 初始：前 period 根 deltas 的 SMA
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # 递推到 closes[-2]（处理 deltas[period..n_deltas-2]）
    for i in range(period, n_deltas - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    rsi_prev = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    # 再走一步到 closes[-1]
    avg_gain = (avg_gain * (period - 1) + gains[n_deltas - 1]) / period
    avg_loss = (avg_loss * (period - 1) + losses[n_deltas - 1]) / period

    rsi_curr = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    return rsi_prev, rsi_curr


def get_weekly_rsi(symbols):
    """获取周线RSI(14) 与 最新已收盘周线 USDT 成交额（分批并发）"""
    params = {"interval": "1w", "limit": 100}

    all_klines = batch_fetch_klines(symbols, params)
    results = {}
    for symbol, klines in all_klines.items():
        if len(klines) < 2:
            continue
        # klines[-1] 是当前未收盘那根；klines[:-1][-1] 即最新已收盘周线
        closed = klines[:-1]
        closed_volume = float(closed[-1][7])

        rsi_prev = None
        rsi_curr = None
        if len(closed) >= 16:
            closes = [float(k[4]) for k in closed]
            rsi_prev, rsi_curr = calc_rsi_last_two(closes)

        results[symbol] = {
            "closedVolume": round(closed_volume, 2),
            "rsiCurr": round(rsi_curr, 6) if rsi_curr is not None else None,
            "rsiPrev": round(rsi_prev, 6) if rsi_prev is not None else None,
        }

    return results


def get_monthly_rsi(symbols):
    """获取月线RSI(14) 与 最新已收盘月线 USDT 成交额（分批并发）"""
    params = {"interval": "1M", "limit": 100}

    all_klines = batch_fetch_klines(symbols, params)
    results = {}
    for symbol, klines in all_klines.items():
        if len(klines) < 2:
            continue
        closed = klines[:-1]
        closed_volume = float(closed[-1][7])

        rsi_prev = None
        rsi_curr = None
        if len(closed) >= 16:
            closes = [float(k[4]) for k in closed]
            rsi_prev, rsi_curr = calc_rsi_last_two(closes)

        results[symbol] = {
            "closedVolume": round(closed_volume, 2),
            "rsiCurr": round(rsi_curr, 6) if rsi_curr is not None else None,
            "rsiPrev": round(rsi_prev, 6) if rsi_prev is not None else None,
        }

    return results


def calc_ema(closes, period):
    """计算 EMA（与 TradingView ta.ema 对齐）。

    Pine 内部从第一根 K 线 source[0] 起递推（非 SMA 初始化），
    在 bar < length-1 时输出 na；等同于 SMA 初始化的旧实现差异主要在
    早期暖机阶段，足够长的窗口（>5x period）下两者收敛到机器精度内。
    本实现采用 first-bar 初始化以严格对齐 Pine。
    """
    if len(closes) < period:
        return None
    alpha = 2 / (period + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = alpha * price + (1 - alpha) * ema
    return ema


def calc_ema_last_two(values, period):
    """单次遍历返回 EMA 序列的 (prev, curr) 两个值，与 calc_ema 同样 first-bar init。"""
    n = len(values)
    if n < period + 1:
        return None, None
    alpha = 2 / (period + 1)
    ema = values[0]
    ema_prev = None
    for i in range(1, n):
        ema_prev = ema
        ema = alpha * values[i] + (1 - alpha) * ema
    return ema_prev, ema


def calc_cvd_last_two(klines, period=14):
    """计算最新已收盘 K 线和上一根的 CVD = cumulative_buying - cumulative_selling。

    与 TradingView Pine "Cumulative Volume Delta" (Ankit_1618) 对齐：
      - 按 K 线形状拆分 buying/selling volume：
          阳线 (close > open): buying = (body% + wick%/2) * vol; selling = wick%/2 * vol
          阴线 (close < open): buying = wick%/2 * vol;            selling = (body% + wick%/2) * vol
          平盘 (close == open): body=0, buying = selling = wick%/2 * vol = vol/2
      - 两个序列分别 EMA(period) 平滑（first-bar init，与 ta.ema 一致）
      - CVD = buying_ema - selling_ema
      - volume 用 k[5] 币本位（与 TV 默认 volume 一致）

    返回 (cvd_prev, cvd_curr)。klines 应为已收盘 K 线列表。
    """
    n = len(klines)
    if n < period + 1:
        return None, None

    buying_vol = []
    selling_vol = []
    for k in klines:
        open_ = float(k[1])
        high = float(k[2])
        low = float(k[3])
        close = float(k[4])
        volume = float(k[5])  # 币本位

        spread = high - low
        if spread == 0:
            # high == low 极端情况，平均拆分
            buying_vol.append(volume / 2)
            selling_vol.append(volume / 2)
            continue

        # 影线长度（与 Pine 公式一致）
        if close > open_:
            upper_wick = high - close
            lower_wick = open_ - low
        else:
            upper_wick = high - open_
            lower_wick = close - low

        body_length = spread - (upper_wick + lower_wick)
        body_pct = body_length / spread
        wick_half_pct = ((upper_wick + lower_wick) / spread) / 2

        if close > open_:
            buying_vol.append((body_pct + wick_half_pct) * volume)
            selling_vol.append(wick_half_pct * volume)
        elif close < open_:
            buying_vol.append(wick_half_pct * volume)
            selling_vol.append((body_pct + wick_half_pct) * volume)
        else:
            # close == open: body=0
            buying_vol.append(wick_half_pct * volume)
            selling_vol.append(wick_half_pct * volume)

    buy_prev, buy_curr = calc_ema_last_two(buying_vol, period)
    sell_prev, sell_curr = calc_ema_last_two(selling_vol, period)
    if buy_prev is None or sell_prev is None:
        return None, None
    return buy_prev - sell_prev, buy_curr - sell_curr


def calc_sar(highs, lows, closes, start=0.02, inc=0.02, max_af=0.2):
    """Parabolic SAR — 严格对齐 TradingView Pine v5 `ta.sar(start, inc, max)`。

    返回 (sar_value, uptrend) 即最新一根 K 的 SAR 数值以及是否处于多头趋势。
    多头时 SAR 在价格下方（close > sar），空头时 SAR 在价格上方。

    实现细节（与 Pine 一致）：
      - bar 1 初始化：close[1] > close[0] 视为多头，SAR=low[0], EP=high[1]；否则反之。
      - 每根递推 SAR := SAR_prev + AF*(EP - SAR_prev)。
      - 多头时 SAR 不得高于 min(low[i-1], low[i-2])；空头镜像。
      - 反转时 SAR 重置为前一段的 EP，再 max/min 到当根+前根的 high/low。
      - 创新极值时更新 EP，AF 累加 inc 上限 max_af；反转时 AF 重置为 start。
    足够长（≥几个反转周期）的窗口下，与 TV 长图 SAR bit 级对齐。
    """
    n = len(closes)
    if n < 2:
        return None, None

    # bar 1 初始化
    if closes[1] > closes[0]:
        uptrend = True
        sar = lows[0]
        ep = highs[1]
    else:
        uptrend = False
        sar = highs[0]
        ep = lows[1]
    af = start

    for i in range(2, n):
        sar = sar + af * (ep - sar)

        if uptrend:
            sar = min(sar, lows[i - 1], lows[i - 2])
            if lows[i] <= sar:
                # 反转为空头
                uptrend = False
                sar = ep
                sar = max(sar, highs[i], highs[i - 1])
                ep = lows[i]
                af = start
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + inc, max_af)
        else:
            sar = max(sar, highs[i - 1], highs[i - 2])
            if highs[i] >= sar:
                # 反转为多头
                uptrend = True
                sar = ep
                sar = min(sar, lows[i], lows[i - 1])
                ep = highs[i]
                af = start
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + inc, max_af)

    return sar, uptrend


def get_daily_indicators(symbols):
    """一次抓取日线 K 线，同时计算多个筛选结果（避免重复请求）。

    返回 (rsi70_data, rsi59_data):
      rsi70_data: {symbol: {rsi, ema9, ema21, ema55, sar, cvdCurr, cvdPrev, volume}}
        筛选: RSI >= 70 + EMA9>21>55 + 百分比间距扩张 + 量>SMA20 + SAR 多头 + CVD 递增
      rsi59_data: {symbol: {rsi, ema9, ema21, ema55, sar, cvdCurr, cvdPrev, volume}}
        筛选: RSI >= 59 + EMA9>21>55 + 百分比间距扩张 + 量>SMA20 + SAR 多头 + CVD 递增
    """
    # limit=499 用于 EMA55 充分暖机，让 EMA 数值与 TradingView 长图精度对齐。
    # Binance fapi /klines 权重按 limit 分桶: [100, 500) = weight 2, [500, 1000] = weight 5。
    # 选 499 即同桶内最大值，IP 权重和 limit=100 完全一致，无限流风险。
    params = {"interval": "1d", "limit": 499}

    all_klines = batch_fetch_klines(symbols, params)
    rsi70_data = {}
    rsi59_data = {}

    for symbol, klines in all_klines.items():
        if len(klines) < 23:
            continue
        closed = klines[:-1]
        if len(closed) < 23:
            continue
        closes = [float(k[4]) for k in closed]
        volume_usdt = float(closed[-1][7])

        ema9 = calc_ema(closes, 9)
        ema21 = calc_ema(closes, 21)
        if ema9 is None or ema21 is None:
            continue

        _, rsi_curr = calc_rsi_last_two(closes)
        if rsi_curr is None:
            continue

        # === RSI59/RSI70：RSI≥阈值 + 三均线多头排列 + 间距扩张 + 量能确认 ===
        if rsi_curr < 59:
            continue

        # 量能确认：最新已收盘日线 **币本位** 成交量 > 近 20 根 SMA
        # 用 k[5] 而非 k[7]，与 TradingView 默认 volume 指标 (币本位) 严格对齐。
        # 注意：列表的 sort/display 仍用 USDT 成交额 (volume_usdt)，与其它 tab 体感一致。
        base_volumes = [float(k[5]) for k in closed]
        if len(base_volumes) < 20:
            continue
        base_vol_ma20 = sum(base_volumes[-20:]) / 20
        if base_volumes[-1] <= base_vol_ma20:
            continue

        if len(closed) < 56:  # 需要算 EMA55 当前值与上一根
            continue
        ema55 = calc_ema(closes, 55)
        if ema55 is None or not (ema9 > ema21 > ema55):
            continue

        # 上一根 K 线收盘时的 EMA（用 closes[:-1]）
        ema9_prev = calc_ema(closes[:-1], 9)
        ema21_prev = calc_ema(closes[:-1], 21)
        ema55_prev = calc_ema(closes[:-1], 55)
        if ema9_prev is None or ema21_prev is None or ema55_prev is None:
            continue

        gap1_curr = (ema9 - ema21) / ema21 * 100
        gap1_prev = (ema9_prev - ema21_prev) / ema21_prev * 100
        gap2_curr = (ema21 - ema55) / ema55 * 100
        gap2_prev = (ema21_prev - ema55_prev) / ema55_prev * 100
        if not (gap1_curr > gap1_prev and gap2_curr > gap2_prev):
            continue

        # SAR 多头确认（与 TradingView Pine `ta.sar(0.02, 0.02, 0.2)` 对齐）
        highs = [float(k[2]) for k in closed]
        lows = [float(k[3]) for k in closed]
        sar_value, sar_uptrend = calc_sar(highs, lows, closes)
        if not sar_uptrend:
            continue

        # CVD 递增：最新已收盘 K 线 CVD > 上一根 CVD
        # （与 TradingView Pine "Cumulative Volume Delta" by Ankit_1618 对齐）
        cvd_prev, cvd_curr = calc_cvd_last_two(closed, period=14)
        if cvd_prev is None or cvd_curr is None or cvd_curr <= cvd_prev:
            continue

        entry = {
            "rsi": round(rsi_curr, 6),
            "ema9": round(ema9, 6),
            "ema21": round(ema21, 6),
            "ema55": round(ema55, 6),
            "sar": round(sar_value, 6),
            "cvdPrev": round(cvd_prev, 6),
            "cvdCurr": round(cvd_curr, 6),
            "volume": round(volume_usdt, 2),
        }
        if rsi_curr >= 70:
            rsi70_data[symbol] = entry
        rsi59_data[symbol] = entry

    return rsi70_data, rsi59_data


def get_funding_rates():
    """获取当前资金费率"""
    data = _api_get(f"{BASE_URL}/fapi/v1/premiumIndex")
    return {
        item["symbol"]: {"fundingRate": float(item["lastFundingRate"]) * 100}
        for item in data
    }


def format_volume(vol):
    """格式化成交量为可读字符串"""
    if vol >= 1e9:
        return f"{vol/1e9:.2f}B"
    elif vol >= 1e6:
        return f"{vol/1e6:.2f}M"
    elif vol >= 1e3:
        return f"{vol/1e3:.2f}K"
    return f"{vol:.2f}"


def build_rankings(symbols, yesterday_data, funding_data, rsi_data, monthly_rsi_data, rsi70_data, rsi59_data):
    """构建排行榜数据"""
    valid_symbols = set(symbols)

    yesterday_change = [
        {
            "symbol": rename_symbol(s),
            "value": d["changePercent"],
            "open": d["open"],
            "close": d["close"],
        }
        for s, d in yesterday_data.items()
        if s in valid_symbols
    ]
    yesterday_change.sort(key=lambda x: x["value"], reverse=True)

    funding_list = [
        {"symbol": rename_symbol(s), "value": round(d["fundingRate"], 5)}
        for s, d in funding_data.items()
        if s in valid_symbols
    ]
    funding_list.sort(key=lambda x: x["value"], reverse=True)

    # 收盘周线RSI - 仅显示递增的，按USDT成交额排序
    weekly_rsi = [
        {
            "symbol": rename_symbol(s),
            "value": yesterday_data[s]["volume"],
            "valueFormatted": format_volume(yesterday_data[s]["volume"]),
            "rsiCurr": v["rsiCurr"],
            "rsiPrev": v["rsiPrev"],
        }
        for s, v in rsi_data.items()
        if s in valid_symbols
        and v.get("rsiCurr") is not None
        and v.get("rsiPrev") is not None
        and v["rsiCurr"] > v["rsiPrev"]
        and s in yesterday_data
    ]
    weekly_rsi.sort(key=lambda x: x["value"], reverse=True)

    # 收盘周线成交额 - 最新已收盘周K线的 USDT 成交额
    weekly_closed_volume = [
        {
            "symbol": rename_symbol(s),
            "value": v["closedVolume"],
            "valueFormatted": format_volume(v["closedVolume"]),
        }
        for s, v in rsi_data.items()
        if s in valid_symbols
    ]
    weekly_closed_volume.sort(key=lambda x: x["value"], reverse=True)

    # 收盘月线成交额 - 最新已收盘月K线的 USDT 成交额
    monthly_closed_volume = [
        {
            "symbol": rename_symbol(s),
            "value": v["closedVolume"],
            "valueFormatted": format_volume(v["closedVolume"]),
        }
        for s, v in monthly_rsi_data.items()
        if s in valid_symbols
    ]
    monthly_closed_volume.sort(key=lambda x: x["value"], reverse=True)

    daily_rsi70 = [
        {
            "symbol": rename_symbol(s),
            "value": d["volume"],
            "valueFormatted": format_volume(d["volume"]),
            "rsi": d["rsi"],
            "ema9": d["ema9"],
            "ema21": d["ema21"],
            "ema55": d["ema55"],
            "sar": d["sar"],
            "cvdPrev": d["cvdPrev"],
            "cvdCurr": d["cvdCurr"],
        }
        for s, d in rsi70_data.items()
        if s in valid_symbols
    ]
    daily_rsi70.sort(key=lambda x: x["value"], reverse=True)

    daily_rsi59 = [
        {
            "symbol": rename_symbol(s),
            "value": d["volume"],
            "valueFormatted": format_volume(d["volume"]),
            "rsi": d["rsi"],
            "ema9": d["ema9"],
            "ema21": d["ema21"],
            "ema55": d["ema55"],
            "sar": d["sar"],
            "cvdPrev": d["cvdPrev"],
            "cvdCurr": d["cvdCurr"],
        }
        for s, d in rsi59_data.items()
        if s in valid_symbols
    ]
    daily_rsi59.sort(key=lambda x: x["value"], reverse=True)

    return {
        "updateTime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "yesterdayChange": yesterday_change,
        "weeklyClosedVolume": weekly_closed_volume[:TOP_N],
        "monthlyClosedVolume": monthly_closed_volume[:TOP_N],
        "fundingRate": funding_list,
        "weeklyRsi": weekly_rsi,
        "dailyRsi70": daily_rsi70,
        "dailyRsi59": daily_rsi59,
    }


def save_data(output):
    os.makedirs("data", exist_ok=True)
    with open("data/rankings.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


FUNDING_INTERVAL = 30  # 资金费率刷新间隔（秒）
FULL_UPDATE_HOUR = 8  # 全量更新时间（UTC+8 早上8点）

# === 周/月数据缓存 ===
# 周线 K 线只在周一 UTC 00:00 切换，月线只在每月 1 号 UTC 00:00 切换
# 缓存避免每小时重复抓取这些低频数据
WEEKLY_CACHE_PATH = "data/cache_weekly.json"
MONTHLY_CACHE_PATH = "data/cache_monthly.json"


def _last_weekly_close_utc():
    """最近一次周线收盘的 UTC 时间（最近一个周一 00:00 UTC）"""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _last_monthly_close_utc():
    """最近一次月线收盘的 UTC 时间（本月 1 号 00:00 UTC）"""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _load_cache(path):
    """读取缓存 JSON，文件缺失/损坏返回 None"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(path, data):
    """写入缓存 JSON"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_daily_data(symbols):
    """抓取每日更新的数据"""
    print("正在获取昨日K线数据...")
    yesterday_data = get_yesterday_change(symbols)

    print("正在获取资金费率...")
    funding_data = get_funding_rates()

    print("正在获取日线指标 (RSI70 + RSI59)...")
    rsi70_data, rsi59_data = get_daily_indicators(symbols)

    return yesterday_data, funding_data, rsi70_data, rsi59_data


def fetch_weekly_data(symbols, force=False):
    """抓取每周更新的数据。仅在最近一次周线收盘后首次运行时抓新数据，否则复用缓存。"""
    cache = _load_cache(WEEKLY_CACHE_PATH)
    last_close = _last_weekly_close_utc()

    if not force and cache:
        try:
            fetched_at = datetime.fromisoformat(cache["fetchedAt"])
            if fetched_at >= last_close and "weeklyRsi" in cache:
                print(f"[周线] 复用缓存 (fetchedAt={cache['fetchedAt']})")
                return cache["weeklyRsi"]
        except (KeyError, ValueError):
            pass  # 缓存格式异常，回退到重新抓

    print("[周线] 抓取周线RSI/成交额...")
    rsi_data = get_weekly_rsi(symbols)

    _save_cache(WEEKLY_CACHE_PATH, {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "weeklyRsi": rsi_data,
    })
    print(f"[周线] 缓存已写入 {WEEKLY_CACHE_PATH}")

    return rsi_data


def fetch_monthly_data(symbols, force=False):
    """抓取每月更新的数据。仅在最近一次月线收盘后首次运行时抓新数据，否则复用缓存。"""
    cache = _load_cache(MONTHLY_CACHE_PATH)
    last_close = _last_monthly_close_utc()

    if not force and cache:
        try:
            fetched_at = datetime.fromisoformat(cache["fetchedAt"])
            if fetched_at >= last_close:
                print(f"[月线] 复用缓存 (fetchedAt={cache['fetchedAt']})")
                return cache["monthlyRsi"]
        except (KeyError, ValueError):
            pass

    print("[月线] 抓取月线RSI/成交额...")
    monthly_rsi_data = get_monthly_rsi(symbols)

    _save_cache(MONTHLY_CACHE_PATH, {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "monthlyRsi": monthly_rsi_data,
    })
    print(f"[月线] 缓存已写入 {MONTHLY_CACHE_PATH}")

    return monthly_rsi_data


def main():
    # === 第一步：启动时抓取全部数据 ===
    print("正在获取合约列表...")
    symbols = get_usdt_perpetual_symbols()
    print(f"共 {len(symbols)} 个USDT永续合约")

    yesterday_data, funding_data, rsi70_data, rsi59_data = fetch_daily_data(symbols)
    rsi_data = fetch_weekly_data(symbols)
    monthly_rsi_data = fetch_monthly_data(symbols)

    output = build_rankings(symbols, yesterday_data, funding_data, rsi_data, monthly_rsi_data, rsi70_data, rsi59_data)
    save_data(output)
    print(f"\n全量数据已保存 | 更新时间: {output['updateTime']}")

    # === 第二步：循环更新 ===
    print(f"\n进入循环模式:")
    print(f"  每 {FUNDING_INTERVAL} 秒更新资金费率")
    print(f"  每天 UTC+8 {FULL_UPDATE_HOUR}:00 延迟1秒更新日线数据")
    print(f"  每周一 UTC+8 {FULL_UPDATE_HOUR}:00 延迟5秒更新周线数据")
    print(f"  每月1号 UTC+8 {FULL_UPDATE_HOUR}:00 更新月线数据")
    print(f"  Ctrl+C 退出")
    last_daily_update_date = datetime.now(timezone(timedelta(hours=8))).date()
    last_weekly_update_week = datetime.now(timezone(timedelta(hours=8))).isocalendar()[1]
    last_monthly_update_month = datetime.now(timezone(timedelta(hours=8))).month

    try:
        while True:
            time.sleep(FUNDING_INTERVAL)
            try:
                now_utc8 = datetime.now(timezone(timedelta(hours=8)))

                # 每天8点更新日线数据
                if now_utc8.hour >= FULL_UPDATE_HOUR and now_utc8.date() > last_daily_update_date:
                    time.sleep(1)
                    print(f"\n[日线更新] {now_utc8.strftime('%Y-%m-%d %H:%M:%S')} UTC+8")

                    # 刷新合约列表
                    symbols = get_usdt_perpetual_symbols()
                    yesterday_data, funding_data, rsi70_data, rsi59_data = fetch_daily_data(symbols)

                    # 周一额外更新周线数据
                    current_week = now_utc8.isocalendar()[1]
                    if now_utc8.weekday() == 0 and current_week != last_weekly_update_week:
                        time.sleep(4)
                        print(f"[周线更新]")
                        rsi_data = fetch_weekly_data(symbols)
                        last_weekly_update_week = current_week
                        print(f"[周线更新完成]")

                    # 每月1号更新月线数据
                    if now_utc8.day == 1 and now_utc8.month != last_monthly_update_month:
                        print(f"[月线更新]")
                        monthly_rsi_data = fetch_monthly_data(symbols)
                        last_monthly_update_month = now_utc8.month
                        print(f"[月线更新完成]")

                    last_daily_update_date = now_utc8.date()
                    print(f"[日线更新完成]")
                else:
                    # 更新资金费率
                    funding_data = get_funding_rates()

                output = build_rankings(symbols, yesterday_data, funding_data, rsi_data, monthly_rsi_data, rsi70_data, rsi59_data)
                save_data(output)
                print(f"[已更新] {output['updateTime']}")
            except Exception as e:
                print(f"[更新失败] {e}")
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
