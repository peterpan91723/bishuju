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


def get_weekly_volume(symbols):
    """通过日K线计算上周成交量（分批并发）"""
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_monday = today.weekday()
    last_monday = today - timedelta(days=days_since_monday + 7)
    last_sunday = last_monday + timedelta(days=7)

    start_time = int(last_monday.timestamp() * 1000)
    end_time = int(last_sunday.timestamp() * 1000)
    params = {"interval": "1d", "startTime": start_time, "endTime": end_time, "limit": 7}

    all_klines = batch_fetch_klines(symbols, params)
    results = {}
    for symbol, klines in all_klines.items():
        total_volume = sum(float(k[7]) for k in klines)
        results[symbol] = round(total_volume, 2)

    return results


def calc_rsi(closes, period=14):
    """计算RSI（Wilder's smoothing，与TradingView一致）"""
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
    return round(100 - 100 / (1 + rs), 2)


def calc_rsi_last_two(closes, period=14):
    """计算最后两个RSI值，用于判断动能递增"""
    if len(closes) < period + 2:
        return None, None
    # 倒数第二个RSI
    rsi_prev = calc_rsi(closes[:-1], period)
    # 最新RSI
    rsi_curr = calc_rsi(closes, period)
    return rsi_prev, rsi_curr


def get_weekly_rsi(symbols):
    """获取周线RSI(14)（分批并发）"""
    params = {"interval": "1w", "limit": 100}

    all_klines = batch_fetch_klines(symbols, params)
    results = {}
    for symbol, klines in all_klines.items():
        if len(klines) >= 17:
            closes = [float(k[4]) for k in klines[:-1]]
            rsi_prev, rsi_curr = calc_rsi_last_two(closes)
            if rsi_curr is not None and rsi_prev is not None:
                results[symbol] = {
                    "rsiCurr": rsi_curr,
                    "rsiPrev": rsi_prev,
                }

    return results


def calc_ema(closes, period):
    """计算EMA（与TradingView一致）"""
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # 初始值用SMA
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def get_daily_rsi_momentum(symbols):
    """获取日线RSI动能递增的币种（分批并发）
    筛选：EMA9 > EMA21 且 最新收盘日线RSI > 上一根收盘日线RSI
    返回：symbol, rsi_curr, rsi_prev, ema9, ema21, volume(昨日USDT成交额)
    """
    params = {"interval": "1d", "limit": 100}

    all_klines = batch_fetch_klines(symbols, params)
    results = {}
    for symbol, klines in all_klines.items():
        if len(klines) >= 23:
            closed = klines[:-1]
            closes = [float(k[4]) for k in closed]

            ema9 = calc_ema(closes, 9)
            ema21 = calc_ema(closes, 21)
            if ema9 is None or ema21 is None or ema9 <= ema21:
                continue

            rsi_prev, rsi_curr = calc_rsi_last_two(closes)
            if rsi_prev is not None and rsi_curr is not None and rsi_curr > rsi_prev:
                volume_usdt = float(closed[-1][7])
                results[symbol] = {
                    "rsiCurr": rsi_curr,
                    "rsiPrev": rsi_prev,
                    "ema9": round(ema9, 6),
                    "ema21": round(ema21, 6),
                    "volume": round(volume_usdt, 2),
                }

    return results


def get_funding_rates():
    """获取当前资金费率"""
    data = _api_get(f"{BASE_URL}/fapi/v1/premiumIndex")
    results = {}
    for item in data:
        symbol = item["symbol"]
        results[symbol] = {
            "fundingRate": float(item["lastFundingRate"]) * 100,
            "nextFundingTime": item["nextFundingTime"],
        }
    return results


def format_volume(vol):
    """格式化成交量为可读字符串"""
    if vol >= 1e9:
        return f"{vol/1e9:.2f}B"
    elif vol >= 1e6:
        return f"{vol/1e6:.2f}M"
    elif vol >= 1e3:
        return f"{vol/1e3:.2f}K"
    return f"{vol:.2f}"


def build_rankings(symbols, yesterday_data, weekly_data, funding_data, rsi_data, momentum_data):
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

    yesterday_volume = [
        {
            "symbol": rename_symbol(s),
            "value": d["volume"],
            "valueFormatted": format_volume(d["volume"]),
        }
        for s, d in yesterday_data.items()
        if s in valid_symbols
    ]
    yesterday_volume.sort(key=lambda x: x["value"], reverse=True)

    weekly_volume = [
        {"symbol": rename_symbol(s), "value": v, "valueFormatted": format_volume(v)}
        for s, v in weekly_data.items()
        if s in valid_symbols
    ]
    weekly_volume.sort(key=lambda x: x["value"], reverse=True)

    funding_list = [
        {"symbol": rename_symbol(s), "value": round(d["fundingRate"], 5)}
        for s, d in funding_data.items()
        if s in valid_symbols
    ]
    funding_list.sort(key=lambda x: x["value"], reverse=True)

    weekly_rsi = [
        {
            "symbol": rename_symbol(s),
            "value": v["rsiCurr"],
            "rsiPrev": v["rsiPrev"],
            "trend": "up" if v["rsiCurr"] > v["rsiPrev"] else "down",
        }
        for s, v in rsi_data.items()
        if s in valid_symbols
    ]
    weekly_rsi.sort(key=lambda x: x["value"], reverse=True)

    rsi_momentum = [
        {
            "symbol": rename_symbol(s),
            "value": d["volume"],
            "valueFormatted": format_volume(d["volume"]),
            "rsiCurr": d["rsiCurr"],
            "rsiPrev": d["rsiPrev"],
        }
        for s, d in momentum_data.items()
        if s in valid_symbols
    ]
    rsi_momentum.sort(key=lambda x: x["value"], reverse=True)

    return {
        "updateTime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "yesterdayChange": yesterday_change,
        "yesterdayVolume": yesterday_volume[:TOP_N],
        "weeklyVolume": weekly_volume[:TOP_N],
        "fundingRate": funding_list,
        "weeklyRsi": weekly_rsi,
        "rsiMomentum": rsi_momentum,
    }


def save_data(output):
    os.makedirs("data", exist_ok=True)
    with open("data/rankings.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


FUNDING_INTERVAL = 30  # 资金费率刷新间隔（秒）
FULL_UPDATE_HOUR = 8  # 全量更新时间（UTC+8 早上8点）


def fetch_daily_data(symbols):
    """抓取每日更新的数据"""
    print("正在获取昨日K线数据...")
    yesterday_data = get_yesterday_change(symbols)

    print("正在获取资金费率...")
    funding_data = get_funding_rates()

    print("正在获取日线RSI动能...")
    momentum_data = get_daily_rsi_momentum(symbols)

    return yesterday_data, funding_data, momentum_data


def fetch_weekly_data(symbols):
    """抓取每周一更新的数据"""
    print("正在获取上周成交量...")
    weekly_data = get_weekly_volume(symbols)

    print("正在获取周线RSI...")
    rsi_data = get_weekly_rsi(symbols)

    return weekly_data, rsi_data


def main():
    # === 第一步：启动时抓取全部数据 ===
    print("正在获取合约列表...")
    symbols = get_usdt_perpetual_symbols()
    print(f"共 {len(symbols)} 个USDT永续合约")

    yesterday_data, funding_data, momentum_data = fetch_daily_data(symbols)
    weekly_data, rsi_data = fetch_weekly_data(symbols)

    output = build_rankings(symbols, yesterday_data, weekly_data, funding_data, rsi_data, momentum_data)
    save_data(output)
    print(f"\n全量数据已保存 | 更新时间: {output['updateTime']}")

    # === 第二步：循环更新 ===
    print(f"\n进入循环模式:")
    print(f"  每 {FUNDING_INTERVAL} 秒更新资金费率")
    print(f"  每天 UTC+8 {FULL_UPDATE_HOUR}:00 延迟1秒更新日线数据")
    print(f"  每周一 UTC+8 {FULL_UPDATE_HOUR}:00 延迟5秒更新周线数据")
    print(f"  Ctrl+C 退出")
    last_daily_update_date = datetime.now(timezone(timedelta(hours=8))).date()
    last_weekly_update_week = datetime.now(timezone(timedelta(hours=8))).isocalendar()[1]

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
                    yesterday_data, funding_data, momentum_data = fetch_daily_data(symbols)

                    # 周一额外更新周线数据
                    current_week = now_utc8.isocalendar()[1]
                    if now_utc8.weekday() == 0 and current_week != last_weekly_update_week:
                        time.sleep(4)
                        print(f"[周线更新]")
                        weekly_data, rsi_data = fetch_weekly_data(symbols)
                        last_weekly_update_week = current_week
                        print(f"[周线更新完成]")

                    last_daily_update_date = now_utc8.date()
                    print(f"[日线更新完成]")
                else:
                    # 更新资金费率
                    funding_data = get_funding_rates()

                output = build_rankings(symbols, yesterday_data, weekly_data, funding_data, rsi_data, momentum_data)
                save_data(output)
                print(f"[已更新] {output['updateTime']}")
            except Exception as e:
                print(f"[更新失败] {e}")
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
