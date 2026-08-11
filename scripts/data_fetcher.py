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

# HTTP/2 补丁（东财接口需要，curl_cffi 可选）
try:
    import curl_cffi.requests as cffi_requests
    import requests as _std_requests
    _std_requests.get = cffi_requests.get
    _std_requests.post = cffi_requests.post
    _std_requests.request = cffi_requests.request
    _std_requests.Session = cffi_requests.Session
    _HTTP2 = True
except ImportError:
    _HTTP2 = False

import requests as requests_mod
import akshare as ak
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

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

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2), retry=retry_if_exception_type(Exception))
def fetch_f10(code, topline=10, year="", month=""):
    """F10 持仓接口"""
    params = {"type": "jjcc", "code": code, "topline": topline}
    if year:
        params["year"], params["month"] = year, month
    r = requests_mod.get(F10_URL, params=params, headers=HEADERS, timeout=20)
    r.encoding = "utf-8"
    m = re.search(r'var apidata=\s*\{\s*content:"(.*?)",\s*arryear', r.text, re.S)
    return m.group(1) if m else r.text

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
    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势", period="近1年")
    df = df.rename(columns={"净值日期": "date", "单位净值": "nav", "日增长率": "growth"})
    df["date"] = pd.to_datetime(df["date"])
    df["growth"] = pd.to_numeric(df["growth"], errors="coerce")
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    if start_date:
        df = df[df["date"] >= pd.Timestamp(start_date)].reset_index(drop=True)
    return df

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2),
       retry=retry_if_exception_type(Exception))
def get_price_df(code, market):
    """个股日线（带全局缓存）"""
    if code in _CACHE:
        return _CACHE[code]
    df = None
    last_err = None
    for attempt in range(3):
        try:
            if market == "US":
                df = ak.stock_us_daily(symbol=code)
            elif market == "HK":
                df = ak.stock_hk_daily(symbol=code)
            elif market == "CN":
                sym = ("sh" if code.startswith("6") else "sz") + code
                df = ak.stock_zh_a_daily(symbol=sym)
            if df is not None and len(df) > 0:
                break
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    if df is None or len(df) == 0:
        _CACHE[code] = None
        if last_err:
            print(f"    [{code}] 行情获取失败: {repr(last_err)[:100]}")
        return None
    df = df.rename(columns={"close": "close"})
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date")
    _CACHE[code] = df
    return df

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2), retry=retry_if_exception_type(Exception))
def get_usdcnh():
    """USDCNH 日线（东财，HTTP/2）"""
    if "__FX__" in _CACHE:
        return _CACHE["__FX__"]
    df = ak.forex_hist_em(symbol="USDCNH")
    df = df.rename(columns={"日期": "date", "最新价": "close"})
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "close"]].sort_values("date").drop_duplicates("date")
    _CACHE["__FX__"] = df
    return df

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2), retry=retry_if_exception_type(Exception))
def get_index(symbol):
    """美股指数（新浪）"""
    key = f"__IDX_{symbol}__"
    if key in _CACHE:
        return _CACHE[key]
    df = ak.index_us_stock_sina(symbol=symbol)
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "close"]].sort_values("date").drop_duplicates("date")
    _CACHE[key] = df
    return df

def get_hs_index():
    """恒生指数（新浪港股）"""
    if "__HSI__" in _CACHE:
        return _CACHE["__HSI__"]
    df = ak.stock_hk_index_daily_sina(symbol="HSI")
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
