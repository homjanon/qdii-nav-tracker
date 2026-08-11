#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDII 基金净值跟踪 - 数据获取模块

所有接口均经本机实测（2026-08-11）：
- F10 持仓：天天基金 FundArchivesDatas.aspx（十大 topline=10 / 年报半年报 topline=100）
- 净值：akshare fund_open_fund_info_em（⚠️ period 参数失效，返回全量，需本地截取）
- 美股：akshare stock_us_daily（新浪源，无日期参数，全量本地过滤）
- 港股：akshare stock_hk_daily（新浪源，5位代码如 02513）
- A股：akshare stock_zh_a_daily（新浪源，sh/sz 前缀）
- 汇率：akshare forex_hist_em USDCNH（需 HTTP/2 补丁 curl_cffi）
- 美股指数：akshare index_us_stock_sina(.NDX/.INX)
- 恒生指数：akshare stock_hk_index_daily_sina('HSI')

市场识别规则（F10 返回无市场前缀）：
- 纯字母（MU/GOOGL/NVDA）→ 美股
- 5位数字 0/1/2 开头（02513 智谱）→ 港股
- 6位 3 开头（300408 三环集团）→ A股
- 其他（285A KIOXIA、005930 三星、000660 SK海力士）→ 无行情源，跳过
"""
import os, re, time, json
import numpy as np
import pandas as pd

# ===== 网络环境修复（必须在导入 requests 之前）=====
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
import socket
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4

# HTTP/2 补丁：仅在获取东财数据（汇率）时临时启用，不做全局替换
# 原因：全局替换 curl_cffi 在 GitHub Actions 云环境偶发 ConnectionError（<Future>），
#      而新浪源（美股/港股/A股/指数）用标准 requests 即可，不需要 HTTP/2。
import requests as requests_mod
try:
    import curl_cffi.requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False

import akshare as ak

F10_URL = "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
           "Referer": "http://fundf10.eastmoney.com/"}

# 全局行情缓存（多基金重仓股去重）
_CACHE = {}

def classify_market(code):
    """代码 → 市场：US / HK / CN / SKIP"""
    if re.fullmatch(r"[A-Za-z]+", code):
        return "US"
    if code.isdigit():
        if len(code) == 5 and code.startswith(("0", "1", "2")):
            return "HK"
        if len(code) == 6 and code.startswith("3"):
            return "CN"
        return "SKIP"
    return "SKIP"

def _retry_call(fn, *args, attempts=3, wait=2.0, label=""):
    """统一重试包装：异常全部吞掉，失败返回 None（不抛，避免拖垮整体）"""
    last_err = None
    for i in range(attempts):
        try:
            return fn(*args)
        except Exception as e:
            last_err = e
            time.sleep(wait * (i + 1))
    if label:
        print(f"    !! {label} 失败: {repr(last_err)[:120]}")
    return None

def fetch_f10(code, topline=10, year="", month=""):
    """F10 持仓接口（HTTP/1.1 即可，无需 curl_cffi）"""
    params = {"type": "jjcc", "code": code, "topline": topline}
    if year:
        params["year"], params["month"] = year, month

    def _fetch():
        r = requests_mod.get(F10_URL, params=params, headers=HEADERS, timeout=20)
        r.encoding = "utf-8"
        m = re.search(r'var apidata=\s*\{\s*content:"(.*?)",\s*arryear', r.text, re.S)
        return m.group(1) if m else r.text

    return _retry_call(_fetch, label=f"F10 {code}")

def parse_holdings(html):
    """取第一个 tbody（最新期）"""
    m = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    body = m.group(1) if m else html
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 9:
            continue
        seq = re.sub(r"<[^>]+>", "", tds[0]).strip()
        if not seq.isdigit():
            continue
        code = re.sub(r"<[^>]+>", "", tds[1]).strip()
        name = re.sub(r"<[^>]+>", "", tds[2]).strip()
        pct = float(re.sub(r"<[^>]+>", "", tds[6]).strip().replace("%", ""))
        out.append({"seq": int(seq), "code": code, "name": name, "pct": pct,
                    "market": classify_market(code)})
    return out

def get_holdings(code, year="", month=""):
    """获取某期十大持仓（默认最新季报）"""
    html = fetch_f10(code, 10, year, month)
    return parse_holdings(html)

def get_nav(code, start_date=None):
    """基金净值（akshare period 参数失效，返回全量后本地截取）
    start_date: 'YYYY-MM-DD' 或 None（返回全部）"""
    def _fetch():
        return ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势", period="近1年")
    df = _retry_call(_fetch, label=f"净值 {code}")
    if df is None or len(df) == 0:
        return df
    df = df.rename(columns={"净值日期": "date", "单位净值": "nav", "日增长率": "growth"})
    df["date"] = pd.to_datetime(df["date"])
    df["growth"] = pd.to_numeric(df["growth"], errors="coerce")
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    if start_date:
        df = df[df["date"] >= pd.Timestamp(start_date)].reset_index(drop=True)
    return df

def get_price_df(code, market):
    """个股日线（带全局缓存），失败返回 None"""
    if code in _CACHE:
        return _CACHE[code]

    def _fetch():
        if market == "US":
            return ak.stock_us_daily(symbol=code)
        if market == "HK":
            return ak.stock_hk_daily(symbol=code)
        if market == "CN":
            sym = ("sh" if code.startswith("6") else "sz") + code
            return ak.stock_zh_a_daily(symbol=sym)
        return None

    df = _retry_call(_fetch, label=f"行情 {code}")
    if df is None or len(df) == 0:
        _CACHE[code] = None
        return None
    df = df.rename(columns={"close": "close"})
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date")
    _CACHE[code] = df
    return df

def get_usdcnh():
    """USDCNH 日线（东财，需 HTTP/2 → curl_cffi 直连；失败降级为 None）"""
    if "__FX__" in _CACHE:
        return _CACHE["__FX__"]
    if not _HAS_CFFI:
        _CACHE["__FX__"] = None
        return None

    def _fetch():
        # 东财 push2 接口要求 HTTP/2：用 curl_cffi 直连，避免全局替换污染新浪源
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": "133.USDCNH", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101", "fqt": "1", "beg": "20240101", "end": "20500101",
        }
        r = cffi_requests.get(url, params=params, timeout=20)
        data = r.json()
        kl = (data.get("data") or {}).get("klines") or []
        rows = []
        for line in kl:
            parts = line.split(",")
            rows.append({"date": parts[0], "close": float(parts[2])})
        return pd.DataFrame(rows)

    df = _retry_call(_fetch, label="USDCNH")
    if df is None or len(df) == 0:
        _CACHE["__FX__"] = None
        print("    !! 汇率源不可用，本次预测不含汇率因子")
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "close"]].sort_values("date").drop_duplicates("date")
    _CACHE["__FX__"] = df
    return df

def get_index(symbol):
    """美股指数（新浪，HTTP/1.1）"""
    key = f"__IDX_{symbol}__"
    if key in _CACHE:
        return _CACHE[key]

    def _fetch():
        return ak.index_us_stock_sina(symbol=symbol)

    df = _retry_call(_fetch, label=f"指数 {symbol}")
    if df is None or len(df) == 0:
        _CACHE[key] = None
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "close"]].sort_values("date").drop_duplicates("date")
    _CACHE[key] = df
    return df

def get_hs_index():
    """恒生指数（新浪港股）"""
    if "__HSI__" in _CACHE:
        return _CACHE["__HSI__"]

    def _fetch():
        return ak.stock_hk_index_daily_sina(symbol="HSI")

    df = _retry_call(_fetch, label="HSI")
    if df is None or len(df) == 0:
        _CACHE["__HSI__"] = None
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "close"]].sort_values("date").drop_duplicates("date")
    _CACHE["__HSI__"] = df
    return df

def asof_ret(prices, nav_dates, lag=0):
    """对每个净值日期 D，取美股 '交易日 <= D-lag' 的最新收盘计算当日收益"""
    p = prices.copy()
    p["ret"] = p["close"].pct_change()
    p = p.dropna(subset=["ret"]).set_index("date")["ret"].sort_index()
    p = p[~p.index.duplicated(keep="last")]
    idx = p.index
    out = []
    for d in nav_dates:
        key = d - pd.Timedelta(days=lag)
        pos = idx.searchsorted(key, side="right") - 1
        out.append(p.iloc[pos] if pos >= 0 else np.nan)
    return np.array(out)

def latest_ret(prices, asof_date):
    """最新可得收益（≤ asof_date 的最后一个交易日收益），用于前瞻预测"""
    return asof_ret(prices, [pd.Timestamp(asof_date)])[0]

def us_trade_dates(start, end):
    """美股交易日列表（NYSE 日历）"""
    import pandas_market_calendars as mcal
    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=start, end_date=end)
    return [pd.Timestamp(d).date() for d in sched.index]
