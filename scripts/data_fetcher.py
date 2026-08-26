#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDII 基金净值跟踪 - 数据获取模块（多源降级版）

数据源降级链（参考 homjanon/portfolio、douban-tracker、cmb-tracker 生产验证过的源）：

- 净值：东财 f10/lsjz 直连（主，portfolio 验证）→ akshare fund_open_fund_info_em（备）
- 汇率：中行牌价 currency_boc_safe（主，portfolio 验证，每日更新）→ 东财 push2his curl_cffi（备）→ yfinance（兜底）
- 美股：yfinance（主，含当天实时，2026-08-19 起首选）→ akshare 新浪日线（兜底）→ 腾讯快照（当日兜底）
- 港股：akshare stock_hk_daily（主）→ 腾讯 qt.gtimg.cn hk 快照（当日兜底）
- A股：akshare stock_zh_a_daily（新浪）
- 美股指数：akshare index_us_stock_sina（新浪）
- 恒生指数：akshare stock_hk_index_daily_sina('HSI')
- F10 持仓：天天基金 FundArchivesDatas.aspx（HTTP/1.1 直连）

market 识别规则（F10 返回无市场前缀）：
- 纯字母（MU/GOOGL/NVDA）→ 美股
- 5位数字 0/1/2 开头（02513 智谱）→ 港股
- 6位 3 开头（300408 三环集团）→ A股
- 其他（285A KIOXIA、005930 三星、000660 SK海力士）→ 无行情源，跳过
"""
import os, re, time, json, datetime
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

import requests as requests_mod
try:
    import curl_cffi.requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False
try:
    import yfinance as yf
    _HAS_YF = True
except ImportError:
    _HAS_YF = False

import akshare as ak

F10_URL = "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
EM_LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"
TX_URL = "https://qt.gtimg.cn/q="
HEADERS_F10 = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
               "Referer": "http://fundf10.eastmoney.com/"}
HEADERS_EM = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
              "Referer": "https://fund.eastmoney.com/"}  # 2026-08-21 修复：fundf10→fund（portfolio 生产验证，lsjz 需此 Referer）
HEADERS_TX = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

# 全局行情缓存（多基金重仓股去重）
_CACHE = {}

# 日韩股代码映射（东财 secid 市场号）：JP=176, KR=177
# 东财搜索实测：铠侠 285A → 176(JPX)、爱德万测试 6857 → 176(JPX)、三星 005930 → 177(KRX)、SK海力士 000660 → 177(KRX)
JP_CODES = {"285A": "KIOXIA", "6857": "爱德万测试"}
KR_CODES = {"005930": "三星电子", "000660": "SK海力士"}
EM_MKT = {"JP": 176, "KR": 177}
TX_PREFIX = {"JP": "jp", "KR": "kr"}

def classify_market(code):
    """代码 → 市场：US / HK / CN / JP / KR / SKIP"""
    if re.fullmatch(r"[A-Za-z]+", code):
        return "US"
    # 日韩股优先（如 285A/6857 日股、005930/000660 韩股）
    if code in JP_CODES:
        return "JP"
    if code in KR_CODES:
        return "KR"
    if code.isdigit():
        if len(code) == 5 and code.startswith(("0", "1", "2")):
            return "HK"
        if len(code) == 6:
            return "CN"  # A股全市场：主板 000/600/601/603/605 + 创业板 300 + 科创板 688（2026-08-26 修复：原来只认 3 开头，误判 600183/603986/688498 为 SKIP）
        return "SKIP"
    return "SKIP"

# 数据源使用统计（可观测性汇总，2026-08-21）
SRC_STATS = {}  # {源名: {"ok": N, "fail": M}}

def _src_record(label, ok):
    """记录数据源使用情况。label 传「源名」即可（如 'yf'/'sina'/'em'/'boc'）"""
    s = SRC_STATS.setdefault(label, {"ok": 0, "fail": 0})
    s["ok" if ok else "fail"] += 1

def _src_key(label):
    """从各种 label 提取纯源名（用于 _retry_call 统计）：
    'yf_MU'→'yf'、'东财lsjz 022184'→'东财lsjz'、'中行牌价'→'中行牌价'、'指数 .NDX'→'指数'"""
    if not label:
        return "?"
    key = label.split("_")[0] if "_" in label else label.split(" ")[0]
    return key

def src_summary():
    """数据源使用汇总：{'yf': ✓12/✗3, 'sina': ✓5, ...}"""
    if not SRC_STATS:
        return "（无数据源调用）"
    parts = []
    for k, v in sorted(SRC_STATS.items()):
        mark = "✓" if v["fail"] == 0 else "✗"
        parts.append(f"{k}={mark}{v['ok']}成功/{v['fail']}失败")
    return " ".join(parts)

def _retry_call(fn, *args, attempts=3, wait=2.0, label="", verbose=False):
    """统一重试包装：异常全部吞掉，失败返回 None（不抛，避免拖垮整体）
    verbose=True 时成功/空也打印（数据源可观测性，2026-08-21）：
      ✓ [label] 成功 (N条) / ⚠ [label] 空数据 / ✗ [label] 失败: 原因"""
    last_err = None
    for i in range(attempts):
        try:
            r = fn(*args)
            if r is not None and (not isinstance(r, pd.DataFrame) or len(r) > 0):
                if label:
                    _src_record(_src_key(label), True)
                if verbose:
                    n = len(r) if isinstance(r, pd.DataFrame) else "?"
                    print(f"    ✓ [{label}] 成功 ({n}条)")
                return r
            if verbose:
                print(f"    ⚠ [{label}] 空数据")
        except Exception as e:
            last_err = e
            time.sleep(wait * (i + 1))
    if label:
        _src_record(_src_key(label), False)
        print(f"    ✗ [{label}] 失败: {repr(last_err)[:120]}")
    return None

def fallback_chain(fetchers, label="", verbose=True):
    """多源降级链：依次尝试，返回首个非 None 结果
    每次尝试都打印数据源结果（数据源可观测性，2026-08-21）：
      ✓ [label/source] 成功 (N条) / ⚠ 空数据 / ✗ 失败: 原因"""
    for name, fn in fetchers:
        try:
            r = fn()
            if r is not None and (not isinstance(r, pd.DataFrame) or len(r) > 0):
                n = len(r) if isinstance(r, pd.DataFrame) else "?"
                _src_record(name, True)  # 源名（yf/sina/em/akshare 等），不含个股代码
                if verbose:
                    print(f"    ✓ [{label}/{name}] 成功 ({n}条)")
                return r
            if verbose:
                print(f"    ⚠ [{label}/{name}] 空数据")
        except Exception as e:
            _src_record(name, False)  # 源名（yf/sina/em/akshare 等）
            print(f"    ✗ [{label}/{name}] 失败: {repr(e)[:100]}")
    return None

# ============ 持仓 ============

def fetch_f10(code, topline=10, year="", month=""):
    """F10 持仓接口（HTTP/1.1 直连，稳定）"""
    params = {"type": "jjcc", "code": code, "topline": topline}
    if year:
        params["year"], params["month"] = year, month

    def _fetch():
        r = requests_mod.get(F10_URL, params=params, headers=HEADERS_F10, timeout=20)
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

# 持仓缓存（F10 偶发超时兜底）：output/holdings_cache.json
# key = f"{code}-{year}-{month}（默认最新期 year='' month='' → key 带 current 标记）"
HOLDINGS_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "holdings_cache.json")
HOLDINGS_CACHE_MAX_AGE_DAYS = 90  # 持仓披露季度更新，90 天缓存足够

def _load_holdings_cache():
    try:
        if os.path.exists(HOLDINGS_CACHE_PATH):
            with open(HOLDINGS_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_holdings_cache(cache):
    try:
        os.makedirs(os.path.dirname(HOLDINGS_CACHE_PATH), exist_ok=True)
        with open(HOLDINGS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def get_holdings(code, year="", month=""):
    """获取某期十大持仓（F10 实时 → 失败读缓存兜底）
    返回 (holdings, source)：source='live' 实时 / 'cache' 缓存
    """
    cache_key = f"{code}|{year or 'current'}|{month or ''}"
    cache = _load_holdings_cache()

    # 实时获取
    html = fetch_f10(code, 10, year, month)
    if html:
        h = parse_holdings(html)
        if h:
            # 成功 → 更新缓存（含时间戳）
            cache[cache_key] = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "year": year, "month": month, "holdings": h}
            _save_holdings_cache(cache)
            return h, "live"

    # 实时失败 → 缓存兜底
    entry = cache.get(cache_key)
    if entry and entry.get("holdings"):
        # 检查缓存时效（默认最新期缓存 90 天内有效）
        try:
            ts = datetime.datetime.strptime(entry["ts"], "%Y-%m-%d %H:%M:%S")
            age_days = (datetime.datetime.now() - ts).days
            if age_days <= HOLDINGS_CACHE_MAX_AGE_DAYS:
                print(f"    !! {code} F10 实时失败，使用持仓缓存（{entry['ts']}，{age_days}天前）")
                return entry["holdings"], "cache"
        except Exception:
            return entry["holdings"], "cache"
    return [], "none"

# ============ 净值（双源：东财 lsjz 直连主 → akshare 备）============

def _em_lsjz(code, page_size=500):
    """东财 f10/lsjz 净值直连（portfolio 生产验证，速度快）"""
    def _fetch():
        r = requests_mod.get(EM_LSJZ_URL,
                             params={"fundCode": code, "pageIndex": 1, "pageSize": page_size},
                             headers=HEADERS_EM, timeout=20)
        d = r.json()
        lst = ((d.get("Data") or {}).get("LSJZList")) or []
        rows = [{"date": x["FSRQ"], "nav": float(x["DWJZ"]),
                 "growth": float(x["JZZZL"]) if x.get("JZZZL") not in (None, "") else np.nan}
                for x in lst]
        return pd.DataFrame(rows)

    return _retry_call(_fetch, label=f"东财lsjz {code}")

def _ak_nav(code):
    """akshare 净值（备源）"""
    def _fetch():
        return ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势", period="近1年")
    df = _retry_call(_fetch, label=f"akshare净值 {code}")
    if df is None or len(df) == 0:
        return None
    df = df.rename(columns={"净值日期": "date", "单位净值": "nav", "日增长率": "growth"})
    df["date"] = pd.to_datetime(df["date"])
    df["growth"] = pd.to_numeric(df["growth"], errors="coerce")
    return df

def get_nav(code, start_date=None):
    """基金净值（akshare 主 → 东财 lsjz 备）
    2026-08-21 提升 akshare 首选：云端(GitHub Actions IP) lsjz 持续被东财限流(30次全失败)，akshare 稳定✓"""
    df = fallback_chain([("akshare", lambda: _ak_nav(code)),
                         ("em_lsjz", lambda: _em_lsjz(code))], label=f"净值{code}")
    if df is None or len(df) == 0:
        return None
    df = df.rename(columns={c: c for c in df.columns})
    if "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    if start_date:
        df = df[df["date"] >= pd.Timestamp(start_date)].reset_index(drop=True)
    return df

def get_fund_purchase(codes):
    """场外基金申购限额（东财 fund_purchase_em，akshare）。
    返回 {code: {"status": str, "limit": float|None}}；接口失败返回 {}（不影响主流程）。
    字段：申购状态（开放申购/限大额/暂停申购/场内交易）、日累计限定金额（元，NaN 表示不限购）。
    2026-08-18 加入（ba7dae3），2026-08-21 从被覆盖状态恢复。"""
    def _fetch():
        df = ak.fund_purchase_em()
        out = {}
        for _, r in df.iterrows():
            c = str(r["基金代码"])
            if c in codes:
                lim = r.get("日累计限定金额")
                out[c] = {
                    "status": str(r.get("申购状态", "")),
                    "limit": float(lim) if lim is not None and str(lim) not in ("nan", "") else None,
                }
        return out
    return _retry_call(_fetch, attempts=2, wait=1.0, label="fund_purchase_em")

# ============ 个股行情（美股 yfinance 主 → 新浪 → 腾讯快照；港A股新浪主）============

def _yf_us_daily(code):
    """美股日线（yfinance / Yahoo，首选，2026-08-19 启用）：
    period="6mo" 的 Close 含当天实时价（美东收盘后即更新），解决新浪美股日线滞后一天的问题。
    2026-08-22 精简：1y→6mo（~130 交易日，足够 60 日 NNLS + 近6月 β 回归，精度无影响）。
    失败（限流/网络）返回 None，由调用方回退新浪日线。"""
    if not _HAS_YF:
        return None

    def _fetch():
        tk = yf.Ticker(code)
        hist = tk.history(period="6mo", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        return pd.DataFrame({
            "date": pd.to_datetime(hist.index.tz_localize(None).date),
            "close": hist["Close"].values,
        })

    return _retry_call(_fetch, attempts=2, wait=1.5, label=f"yf_{code}", verbose=True)

def _sina_price(code, market):
    """akshare 新浪源日线"""
    if market == "US":
        return ak.stock_us_daily(symbol=code)
    if market == "HK":
        return ak.stock_hk_daily(symbol=code)
    if market == "CN":
        sym = ("sh" if code.startswith("6") else "sz") + code
        return ak.stock_zh_a_daily(symbol=sym)
    return None

# ============ 日韩股行情（东财 push2his 主 → yfinance 备 → 腾讯快照兜底）============

def _em_jpkr(code, market):
    """东财 push2his 日韩股历史K线（secid=市场号.代码：JP=176 / KR=177）
    返回 DataFrame(date, close) 或 None"""
    secid = f"{EM_MKT[market]}.{code}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

    def _fetch():
        # 尝试多个 push2his 域名（个别偶发断连）
        for host in ("push2his.eastmoney.com", "22.push2his.eastmoney.com",
                     "19.push2his.eastmoney.com"):
            try:
                r = requests_mod.get(
                    f"https://{host}/api/qt/stock/kline/get",
                    params={"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                            "fields2": "f51,f52,f53,f54,f55,f56,f57",
                            "klt": "101", "fqt": "1", "beg": "20240101", "end": "20500101"},
                    headers=headers, timeout=15)
                if r.status_code != 200:
                    continue
                d = r.json()
                kl = ((d.get("data") or {}).get("klines")) or []
                if kl:
                    rows = []
                    for line in kl:
                        parts = line.split(",")
                        rows.append({"date": parts[0], "close": float(parts[2])})
                    df = pd.DataFrame(rows)
                    # 2026-08-22 精简：仅保留近 200 个交易日（足够 NNLS+β），防全量占用
                    return df.tail(200) if len(df) > 200 else df
            except Exception:
                continue
        return None

    return _retry_call(_fetch, label=f"东财{market} {code}", attempts=2, wait=1.5, verbose=True)

def _yf_jpkr(code, market):
    """yfinance 日韩股（首选，2026-08-21 起：285A.T / 005930.KS / 000660.KS）
    2026-08-22 精简：固定 start→period='6mo'（~130 交易日，足够 NNLS+β 回归）"""
    if not _HAS_YF:
        return None
    suffix = ".T" if market == "JP" else ".KS"
    sym = code + suffix

    def _fetch():
        t = yf.Ticker(sym)
        hist = t.history(period="6mo", auto_adjust=False)
        if hist is None or len(hist) == 0:
            return None
        df = hist.reset_index()[["Date", "Close"]].rename(
            columns={"Date": "date", "Close": "close"})
        df["date"] = pd.to_datetime(df["date"])
        # 2026-08-26 修复：yfinance 返回 tz-aware 日期，与新浪(朴素)比较崩溃
        # （012922 等含日韩股基金报 Cannot compare tz-naive and tz-aware）
        if getattr(df["date"].dtype, "tz", None) is not None:
            df["date"] = df["date"].dt.tz_localize(None)
        return df

    return _retry_call(_fetch, label=f"yf{market} {code}", attempts=2, wait=2.0, verbose=True)

def _tencent_jpkr_snapshot(code, market):
    """腾讯日韩股快照（当日预测兜底，kr005930 / jp285A）"""
    prefix = TX_PREFIX.get(market)
    if not prefix:
        return None

    def _fetch():
        r = requests_mod.get(TX_URL + f"{prefix}{code}", headers=HEADERS_TX, timeout=15)
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            if "=" not in line:
                continue
            parts = line.split("=", 1)[1].strip().strip('"').split("~")
            if len(parts) < 5:
                return None
            try:
                price = float(parts[3])
                prev = float(parts[4])
            except (ValueError, TypeError):
                return None
            if price <= 0:
                return None
            today = pd.Timestamp.now().normalize()
            prev_date = today - pd.Timedelta(days=1)
            return pd.DataFrame({"date": [prev_date, today], "close": [prev, price]})
        return None

    return _retry_call(_fetch, label=f"腾讯{market} {code}", attempts=2, wait=1.0)

def _tencent_snapshot_df(code, market):
    """腾讯实时快照 → 构造仅含「昨日收盘/最新收盘」两行的迷你日线（当日预测兜底）
    腾讯 [3]=最新价 [4]=昨收 [32]=涨跌幅% → 构造 [昨日, 今日] 两日收盘序列"""
    prefix = {"US": "us", "HK": "hk", "CN": ("sh" if code.startswith("6") else "sz")}.get(market)
    if not prefix:
        return None

    def _fetch():
        r = requests_mod.get(TX_URL + f"{prefix}{code}", headers=HEADERS_TX, timeout=15)
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            if "=" not in line:
                continue
            parts = line.split("=", 1)[1].strip().strip('"').split("~")
            if len(parts) < 33:
                return None
            try:
                price = float(parts[3])
                prev = float(parts[4])
            except (ValueError, TypeError):
                return None
            today = pd.Timestamp.now().normalize()
            prev_date = today - pd.Timedelta(days=1)
            return pd.DataFrame({"date": [prev_date, today], "close": [prev, price]})
        return None

    return _retry_call(_fetch, label=f"腾讯快照 {code}", attempts=2, wait=1.0)

def get_price_df(code, market, allow_snapshot=True):
    """个股日线：
    - US/HK/CN: 新浪主源（完整历史）→ 腾讯快照兜底（仅当日，预测够用）
    - JP/KR:     东财 push2his 主（历史K线）→ yfinance 备 → 腾讯快照兜底（当日）
    返回 DataFrame(date, close)；快照模式返回的只有最近两日。
    """
    if code in _CACHE:
        return _CACHE[code]

    if market == "US":
        # 美股：yfinance 主（含当天实时，2026-08-19 起首选）→ 新浪日线兜底
        df = fallback_chain([("yf", lambda: _yf_us_daily(code)),
                             ("sina", lambda: _sina_price(code, market))],
                            label=f"美股{code}")
    elif market in ("JP", "KR"):
        # 日韩股：yfinance 首选（2026-08-21 提升，云端稳定 .T/.KS）→ 东财备源 → 腾讯快照兜底
        df = fallback_chain([("yf", lambda: _yf_jpkr(code, market)),
                             ("em", lambda: _em_jpkr(code, market))],
                            label=f"日韩{code}")
    else:
        df = fallback_chain([("sina", lambda: _sina_price(code, market))], label=f"行情{code}")
    if df is not None and len(df) > 0:
        df = df.rename(columns={"close": "close"})
        df = df[["date", "close"]].copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").drop_duplicates("date")
        _CACHE[code] = df
        return df

    # 主源失败 → 腾讯快照兜底（仅当日预测用）
    if allow_snapshot:
        snap = _tencent_jpkr_snapshot(code, market) if market in ("JP", "KR") \
            else _tencent_snapshot_df(code, market)
        if snap is not None and len(snap) > 0:
            print(f"    [{code}] 主源失败，使用腾讯快照兜底（仅当日）")
            _CACHE[code] = snap
            return snap

    _CACHE[code] = None
    return None

# ============ 汇率（中行牌价主 → 东财 push2his 备 → yfinance 兜底）============

def _boc_fx():
    """中行牌价（portfolio 生产验证）：美元列 ÷100 = USD/CNY，每日更新，1994 至今"""
    def _fetch():
        df = ak.currency_boc_safe()
        df = df[["日期", "美元"]].dropna()
        df = df.rename(columns={"日期": "date", "美元": "usd"})
        df["date"] = pd.to_datetime(df["date"])
        df["close"] = df["usd"] / 100.0
        return df[["date", "close"]]

    return _retry_call(_fetch, label="中行牌价", verbose=True)

def _em_fx():
    """东财 push2his USDCNH（备，需 HTTP/2）"""
    if not _HAS_CFFI:
        return None

    def _fetch():
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

    return _retry_call(_fetch, label="东财USDCNH", verbose=True)

def _yf_fx():
    """yfinance USDCNH=X（兜底，GitHub Actions 云端 IP 较干净）"""
    if not _HAS_YF:
        return None

    def _fetch():
        t = yf.Ticker("USDCNH=X")
        hist = t.history(start="2025-06-01", end="2026-12-31", auto_adjust=False)
        if hist is None or len(hist) == 0:
            return None
        df = hist.reset_index()[["Date", "Close"]].rename(
            columns={"Date": "date", "Close": "close"})
        df["date"] = pd.to_datetime(df["date"])
        return df

    return _retry_call(_fetch, label="yfinance USDCNH", attempts=2, wait=2.0, verbose=True)

def get_usdcnh():
    """USD/CNH 日线：中行牌价主 → 东财 push2his 备 → yfinance 兜底"""
    if "__FX__" in _CACHE:
        return _CACHE["__FX__"]

    df = fallback_chain([("中行牌价", _boc_fx),
                         ("东财USDCNH", _em_fx),
                         ("yfinance", _yf_fx)], label="汇率")
    if df is None or len(df) == 0:
        _CACHE["__FX__"] = None
        print("    !! 汇率源全部不可用，本次预测不含汇率因子")
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "close"]].sort_values("date").drop_duplicates("date")
    _CACHE["__FX__"] = df
    print(f"    汇率源: {len(df)} 行, 最新 {df['date'].max().date()} {df['close'].iloc[-1]:.4f}")
    return df

# ============ 指数 ============

def _yf_index(symbol):
    """美股指数（yfinance，首选，2026-08-22 起）：
    与美股个股同源同步（新浪清晨滞后一天 → 背离误判，已修复）
    symbol: '.NDX'→'^NDX'、'.INX'→'^GSPC'；period='6mo' 足够 β 回归（~130 交易日）"""
    if not _HAS_YF:
        return None
    ysym = {"^NDX": "^NDX", ".NDX": "^NDX", ".INX": "^GSPC", "^GSPC": "^GSPC"}.get(symbol, symbol)

    def _fetch():
        tk = yf.Ticker(ysym)
        hist = tk.history(period="6mo", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        return pd.DataFrame({
            "date": pd.to_datetime(hist.index.tz_localize(None).date),
            "close": hist["Close"].values,
        })

    return _retry_call(_fetch, attempts=2, wait=1.5, label=f"yf指数 {symbol}", verbose=True)

def get_index(symbol):
    """美股指数（yfinance 首选 → 新浪兜底）
    2026-08-22 修复：新浪清晨滞后一天导致 NDX 与个股不同步 → 背离误判"""
    key = f"__IDX_{symbol}__"
    if key in _CACHE:
        return _CACHE[key]

    df = fallback_chain([("yf", lambda: _yf_index(symbol)),
                         ("sina", lambda: ak.index_us_stock_sina(symbol=symbol))],
                        label=f"指数 {symbol}")
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

    df = _retry_call(_fetch, label="HSI", verbose=True)
    if df is None or len(df) == 0:
        _CACHE["__HSI__"] = None
        return None
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "close"]].sort_values("date").drop_duplicates("date")
    _CACHE["__HSI__"] = df
    return df

# ============ 收益对齐 ============

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

def us_last_trade_date():
    """美股最近一个已收盘交易日（美东视角，统一预测基准）
    规则：净值日期 D 对应美股「交易日 ≤ D」最新收盘（lag=0）。
    先检查「美东今天」是否已收盘（UTC 视角：美东收盘=UTC 20:00），
    是则返回今天；否则往前找最近已收盘交易日。
    返回: datetime.date
    """
    import datetime as _dt
    import pandas_market_calendars as mcal
    nyse = mcal.get_calendar("NYSE")
    now_utc = _dt.datetime.now(_dt.timezone.utc)

    # 先看美东今天（UTC 日期，美股收盘于 UTC 20:00 → 当天 UTC 若已过 20:00 即已收盘）
    today = now_utc.date()
    for offset in range(0, 6):  # 0=今天, 1=昨天, ... 覆盖周末
        d = (now_utc - _dt.timedelta(days=offset)).date()
        try:
            sched = nyse.schedule(start_date=str(d), end_date=str(d))
            if len(sched) > 0:
                close_utc = sched.iloc[0]["market_close"]
                if now_utc >= close_utc.to_pydatetime().replace(tzinfo=_dt.timezone.utc):
                    return d
        except Exception:
            continue
    # 兜底：最近一个工作日
    d = now_utc.date()
    while d.weekday() >= 5:
        d -= _dt.timedelta(days=1)
    return d
