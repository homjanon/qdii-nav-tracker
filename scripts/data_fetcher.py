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

HOLDINGS_TOP_N = 20  # 2026-09-05 升级：十大→二十大（覆盖率 47.8%→68.8%，NNLS 维度 21 < 60 窗口安全）

def fetch_f10(code, topline=20, year="", month=""):
    """F10 持仓接口（HTTP/1.1 直连，稳定）
    默认取前 20 大（HOLDINGS_TOP_N=20，2026-09-05 升级：原十大覆盖率 ~48% → 二十大 ~69%）"""
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
        pct_txt = re.sub(r"<[^>]+>", "", tds[6]).strip().replace("%", "").replace(",", "")
        try:
            pct = float(pct_txt)
        except ValueError:
            # 2026-09-05 容错：占比为 '---'/空 等占位（部分基金披露格式），该行占比按 0 处理
            pct = 0.0
        out.append({"seq": int(seq), "code": code, "name": name, "pct": pct,
                    "market": classify_market(code)})
    return out

# ============ 持仓报告期校验（2026-09-18 加入）============
# 背景：ETF 联接基金 / FOF 等特殊基金的 F10「股票投资明细」可能长期停更
#   （实例 017093 景顺纳科C：数据停在 2023Q3，解析出的 20 条为跨期混合垃圾，
#    且 analyze_fund 的"空持仓保护"不触发 → 会静默产出错误预测）。
# 规则：解析 F10 最新报告期，与"当前应已披露期（留 4 个月缓冲）"比较，
#   落后超过 HOLDING_STALE_QUARTERS 个季度 → 判定过期，跳过预测并告警。
HOLDING_STALE_QUARTERS = 2

def parse_report_period(html):
    """从 F10 HTML 解析最新报告期 → (year, quarter)；解析不到返回 None。
    形如 '2026年2季度股票投资明细'（页面按最新期在前排序）。"""
    m = re.search(r"(20\d{2})年(\d)季度", html or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def _expected_period(d=None, lag_months=4):
    """当前日期下最保守的"应已披露"报告期（往前留 lag_months 个月缓冲）"""
    d = d or datetime.date.today()
    y, m = d.year, d.month - lag_months
    while m <= 0:
        m += 12
        y -= 1
    return y, (m - 1) // 3 + 1

def is_period_stale(period, max_lag=HOLDING_STALE_QUARTERS):
    """报告期是否过期（比"应已披露期"落后超过 max_lag 个季度）。
    period 为 None（解析不到）时返回 False（保守放行，兼容无期次标记的返回）。"""
    if not period:
        return False
    exp = _expected_period()
    lag = (exp[0] * 4 + exp[1]) - (period[0] * 4 + period[1])
    return lag > max_lag

def get_holdings_period(code):
    """获取 F10 最新报告期（轻量请求 topline=5）→ (year, quarter) 或 None"""
    try:
        html = fetch_f10(code, 5)
        return parse_report_period(html)
    except Exception:
        return None

def fetch_f10_all(code, year="2026", month="6"):
    """拉取某期全部股票持仓（topline=1000，覆盖 021277 805 只等全量）
    返回解析后的全持仓列表（纯标的+占比，不含行情）"""
    html = fetch_f10(code, topline=1000, year=year, month=month)
    if not html:
        return []
    return parse_holdings(html)

# 全持仓静态缓存（中报/年报披露后抓一次，非每日行情）
# 2026-09-05 修复：缓存文件与报告文件分离 —— 报告 holdings_full.json 是
# {report_date, funds} 结构（render 用）；此处私有缓存用独立文件存 {code: {...}}
# 结构，避免两种结构互相覆盖导致 120 天缓存失效、天天重拉全持仓。
HOLDINGS_FULL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "holdings_full.json")
HOLDINGS_FULL_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", ".holdings_full_cache.json")
HOLDINGS_FULL_MAX_AGE_DAYS = 120  # 披露期（8月底中报/3月底年报）才刷新，120 天足够

def _load_full_cache():
    try:
        if os.path.exists(HOLDINGS_FULL_CACHE_PATH):
            with open(HOLDINGS_FULL_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_full_cache(cache):
    try:
        os.makedirs(os.path.dirname(HOLDINGS_FULL_CACHE_PATH), exist_ok=True)
        with open(HOLDINGS_FULL_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def get_holdings_full(code, force=False):
    """获取某基金全部持仓（静态，披露期缓存；force=True 强制刷新）
    返回 (holdings, source)：source='live' / 'cache' / 'none'
    holdings = [{seq, code, name, pct, market}, ...] 全量
    """
    cache = _load_full_cache()
    entry = cache.get(code)

    # 缓存有效（未过期 且 非强制）→ 直接用
    if not force and entry and entry.get("holdings"):
        try:
            ts = datetime.datetime.strptime(entry["ts"], "%Y-%m-%d %H:%M:%S")
            age_days = (datetime.datetime.now() - ts).days
            if age_days <= HOLDINGS_FULL_MAX_AGE_DAYS:
                return entry["holdings"], "cache"
        except Exception:
            return entry["holdings"], "cache"

    # 拉实时全持仓（中报 2026/6；失败退缓存兜底）
    h = fetch_f10_all(code)
    if h:
        cache[code] = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "year": "2026", "month": "6", "holdings": h,
                       "count": len(h)}
        _save_full_cache(cache)
        return h, "live"
    if entry and entry.get("holdings"):
        return entry["holdings"], "cache"
    return [], "none"

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
    """获取某期二十大持仓（F10 实时 → 失败读缓存兜底，2026-09-05 升级 10→20）
    返回 (holdings, source)：source='live' 实时 / 'cache' 缓存
    """
    cache_key = f"{code}|{year or 'current'}|{month or ''}"
    cache = _load_holdings_cache()

    # 实时获取
    html = fetch_f10(code, HOLDINGS_TOP_N, year, month)
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
        hist = _yf_history(tk, auto_adjust=True)
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
        hist = _yf_history(t, auto_adjust=False)
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
        # 缺口补齐（2026-09-24）：以 yfinance 为主源的市场（US/JP/KR）走 东财 → 新浪 → 腾讯快照
        if market == "US":
            _fx = [("东财", lambda: _em_us_stock_series(code)),
                   ("新浪", lambda: _sina_series(code, "US")),
                   ("腾讯", lambda: _tencent_series(code, "US"))]
        elif market in ("JP", "KR"):
            _fx = [("新浪", lambda: _sina_series(code, market)),
                   ("腾讯", lambda: _tencent_series(code, market))]
        else:
            _fx = None
        if _fx:
            df, _ = _repair_missing_days(df, market, code, _fx)
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

def _yf_history(tk, period="6mo", auto_adjust=True):
    """yfinance history() 统一入口（2026-09-24）。

    keepna=True 是关键：Yahoo 偶发返回「整行空值」的日K（实例 2026-09-22 的 ^NDX / ASML），
    yfinance 默认 keepna=False 会把这类行**静默丢弃** → 序列出现交易日缺口 →
    下游「相邻两行 = 相邻交易日」的假设失效（事故：NDX 显示 -0.04%，实为 -0.85%；
    ASML 算出 +1.95%，实为 -0.19%，方向都反了）。
    保留空行后交由 _repair_missing_days 补齐 / 置 NaN。

    兼容处理：若 yfinance 版本过老不认识 keepna 参数 → 自动降级（不阻断主流程）。
    """
    try:
        return tk.history(period=period, auto_adjust=auto_adjust, keepna=True)
    except TypeError:
        return tk.history(period=period, auto_adjust=auto_adjust)


def _yf_index(symbol):
    """美股指数（yfinance，首选，2026-08-22 起）：
    与美股个股同源同步（新浪清晨滞后一天 → 背离误判，已修复）
    symbol: '.NDX'→'^NDX'、'.INX'→'^GSPC'；period='6mo' 足够 β 回归（~130 交易日）

    ⚠️ keepna=True（2026-09-24 起，关键）：Yahoo 偶发返回「整行空值」的日K（实例：
    2026-09-22 的 ^NDX / ASML 全 None）。yfinance 默认 keepna=False 会把这类行
    **静默丢弃** → 序列出现交易日缺口 → 下游「相邻两行=相邻交易日」的假设失效
    （事故：NDX 显示 -0.04%，实为 -0.85%）。保留空行后由 _repair_missing_days 补齐。"""
    if not _HAS_YF:
        return None
    ysym = {"^NDX": "^NDX", ".NDX": "^NDX", ".INX": "^GSPC", "^GSPC": "^GSPC"}.get(symbol, symbol)

    def _fetch():
        tk = yf.Ticker(ysym)
        hist = _yf_history(tk, auto_adjust=True)
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
    # 缺口补齐（2026-09-24）：东财 → 新浪 → 腾讯快照
    df, _ = _repair_missing_days(df, "US", f"指数 {symbol}", [
        ("东财", lambda: _em_index_series(symbol)),
        ("新浪", lambda: _sina_series(symbol, "US")),
        ("腾讯", lambda: _tencent_series(symbol, "US", symbol=symbol)),
    ])
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

# ============ 价格序列缺口检测与补齐（2026-09-24）============
# 背景：Yahoo 偶发返回「整行空值」的日K（实例：2026-09-22 的 ^NDX / ASML 全 None），
#   yfinance 默认 keepna=False 会**静默丢弃**该行 → 序列出现交易日缺口 →
#   任何「相邻两行 = 相邻交易日」的假设（close[-1]/close[-2]、pct_change）都会
#   把多日累计当单日。事故：NDX 显示 -0.04%（实为 -0.85%）；ASML 算出 +1.95%（实为 -0.19%，方向都反了）。
# 处置：① yfinance 全部改 keepna=True（空行不再无声消失）
#   ② 本模块用市场交易日历检测缺口 → 按 东财 → 新浪 → 腾讯快照 顺序补齐
#   ③ 补到的记 repaired；全都取不到 → 记 unresolved，上层跳过该标的当日预测并告警

DATA_QUALITY = {"found": [], "repaired": [], "unresolved": []}

# 只对「最近 N 个自然日」内的缺口做联网补齐（足够当日预测+页面展示，避免历史长尾拖慢）
GAP_WINDOW_DAYS = 35
# 单标的近期缺口上限：超过视为「日历不匹配 / 长期停牌」，不做占位，仅告警
GAP_MAX_PER_SYMBOL = 12
# 需要做缺口补齐的市场：以 yfinance 为主源的市场（HK/CN 主源是新浪，历史完整，不动）
REPAIR_MARKETS = {"US", "JP", "KR"}

_TRADING_DAY_CACHE = {}
_MKT_CAL_NAME = {"US": "NYSE", "HK": "XHKG", "CN": "XSHG", "JP": "JPX", "KR": "XKRX"}


def trading_day_set(market, lookback_days=420):
    """某市场近 N 天的交易日集合 set[datetime.date]；不支持的市场返回 None（调用方据此跳过检查）"""
    key = (market, lookback_days)
    if key in _TRADING_DAY_CACHE:
        return _TRADING_DAY_CACHE[key]
    name = _MKT_CAL_NAME.get(market)
    out = None
    if name:
        try:
            import pandas_market_calendars as mcal
            cal = mcal.get_calendar(name)
            end = datetime.date.today() + datetime.timedelta(days=1)
            start = end - datetime.timedelta(days=lookback_days)
            days = cal.valid_days(start_date=str(start), end_date=str(end))
            out = {pd.Timestamp(d).date() for d in days}
        except Exception as e:
            print(f"    ⚠ [缺口检测] 日历 {name} 不可用: {repr(e)[:80]}")
    _TRADING_DAY_CACHE[key] = out
    return out


def _em_kline_series(secid, lmt=45):
    """东财 push2his 日线 → {date: close}（需 curl_cffi；境内源不可走代理）"""
    def _fetch():
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                  "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(lmt)}
        if _HAS_CFFI:
            r = cffi_requests.get(url, params=params, headers=HEADERS_EM,
                                  impersonate="chrome", timeout=20)
        else:
            r = requests_mod.get(url, params=params, headers=HEADERS_EM, timeout=20)
        data = (r.json() or {}).get("data") or {}
        out = {}
        for line in (data.get("klines") or []):
            p = line.split(",")
            try:
                out[pd.Timestamp(p[0]).date()] = float(p[2])
            except Exception:
                continue
        return out or None

    return _retry_call(_fetch, attempts=2, wait=1.0, label="东财补缺口", verbose=False)


def _em_us_stock_series(code, lmt=45):
    """美股个股东财日线：市场号 105=NASDAQ / 106=NYSE / 107=AMEX，依次尝试"""
    for mkt in (105, 106, 107):
        s = _em_kline_series(f"{mkt}.{code}", lmt=lmt)
        if s:
            return s
    return None


# ⚠️ 地雷：东财 100.NDX = 纳斯达克**综合**指数；纳指100 必须用 100.NDX100
_EM_INDEX_SECID = {".NDX": "100.NDX100", "^NDX": "100.NDX100",
                   ".INX": "100.SPX", "^GSPC": "100.SPX",
                   ".DJI": "100.DJIA", "^DJI": "100.DJIA"}


def _em_index_series(symbol, lmt=45):
    secid = _EM_INDEX_SECID.get(symbol)
    return _em_kline_series(secid, lmt=lmt) if secid else None


def _sina_series(code, market):
    """新浪日线 → {date: close}（美股/指数为全历史，是缺口补齐的主力兜底）"""
    def _fetch():
        if code.startswith(".") or code.startswith("^"):
            df = ak.index_us_stock_sina(symbol=code)
        else:
            df = _sina_price(code, market)
        if df is None or len(df) == 0:
            return None
        cols = {str(c).lower(): c for c in df.columns}
        dc, cc = cols.get("date"), cols.get("close")
        if not dc or not cc:
            return None
        dd = pd.to_datetime(df[dc], errors="coerce")
        out = {}
        for t, v in zip(dd, df[cc]):
            if pd.isna(t) or pd.isna(v):
                continue
            try:
                out[pd.Timestamp(t).date()] = float(v)
            except Exception:
                continue
        return out or None

    return _retry_call(_fetch, attempts=2, wait=1.0, label="新浪补缺口", verbose=False)


_TX_INDEX_CODE = {".NDX": "usNDX", "^NDX": "usNDX", ".INX": "usINX", "^GSPC": "usINX"}


def _tencent_series(code, market, symbol=None):
    """腾讯快照 → {date: close}（只能提供「最新一日」+「前一交易日」两个点，作最后兜底）"""
    q = _TX_INDEX_CODE.get(symbol or "") or (("us" + code) if market == "US" else None)
    if not q:
        return None

    def _fetch():
        r = requests_mod.get(TX_URL + q, headers=HEADERS_TX, timeout=15)
        r.encoding = "gbk"
        txt = (r.text or "").strip()
        if "=" not in txt:
            return None
        parts = txt.split("=", 1)[1].strip().strip('"').split("~")
        if len(parts) < 33:
            return None
        cur, prev = float(parts[3]), float(parts[4])
        m = re.search(r"(20\d{2}-\d{2}-\d{2})", txt)
        if not m:
            return None
        d1 = pd.Timestamp(m.group(1)).date()
        out = {d1: cur}
        cal = trading_day_set(market)
        if cal:
            prevs = sorted(x for x in cal if x < d1)
            if prevs:
                out[prevs[-1]] = prev
        return out or None

    return _retry_call(_fetch, attempts=2, wait=1.0, label="腾讯补缺口", verbose=False)


def _repair_missing_days(df, market, key, fetchers):
    """检测并补齐价格序列的交易日缺口。

    参数：
      df       DataFrame(date, close)
      market   US/JP/KR（其它市场直接原样返回）
      key      记录用标识（如 'ASML' / '指数 .NDX'）
      fetchers [(源名, callable() -> {date: close} | None), ...] 按序补齐
    返回 (df, unresolved_dates)：最近窗口内每个交易日都有一行；
      补不到的以 close=NaN **占位** —— 让下游拿到 NaN，而不是伪造的两日累计。
    """
    if df is None or len(df) == 0 or market not in REPAIR_MARKETS:
        return df, []
    cal = trading_day_set(market)
    if not cal:
        return df, []

    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    d = d.drop_duplicates("date", keep="last").sort_values("date")
    dmin, dmax = d["date"].min().date(), d["date"].max().date()
    have = set(d["date"].dt.date)

    win_start = datetime.date.today() - datetime.timedelta(days=GAP_WINDOW_DAYS)
    # ⚠️ 必须与序列自身起点 dmin 求交：序列覆盖范围之外的日期不算缺口
    # （否则"新上市/序列较短"的标的会被误判为大量缺口）
    win_lo = max(win_start, dmin)
    need = sorted({x for x in cal if win_lo <= x <= dmax and x not in have}
                  | set(d.loc[d["close"].isna(), "date"].dt.date))
    if not need:
        hist_missing = sorted(x for x in cal if dmin <= x <= dmax and x not in have)
        if hist_missing:
            for x in hist_missing:
                DATA_QUALITY["found"].append({"symbol": key, "date": str(x), "scope": "history"})
            print(f"    ⚠ [缺口] {key} 历史区间缺 {len(hist_missing)} 个交易日（超出补齐窗口，仅记录）")
        return d.reset_index(drop=True), []

    for x in need:
        DATA_QUALITY["found"].append({"symbol": key, "date": str(x), "scope": "recent"})

    if len(need) > GAP_MAX_PER_SYMBOL:
        print(f"    ⚠ [缺口] {key} 近期缺 {len(need)} 天（>{GAP_MAX_PER_SYMBOL}）"
              f"→ 疑日历不匹配/长期停牌，不占位也不补齐，仅登记告警")
        for x in need:
            DATA_QUALITY["unresolved"].append({"symbol": key, "date": str(x), "scope": "capped"})
        return d.reset_index(drop=True), need

    # ---- 依次尝试备用源（同一个源只请求一次，批量补多天）----
    got = {}
    for name, fn in fetchers:
        still = [x for x in need if x not in got]
        if not still:
            break
        try:
            series = fn()
        except Exception as e:
            print(f"    ✗ [缺口补齐 {key}/{name}] {repr(e)[:70]}")
            continue
        if not series:
            continue
        before = len(got)
        for x in still:
            v = series.get(x)
            if v is not None and pd.notna(v) and float(v) > 0:
                got[x] = float(v)
        if len(got) > before:
            print(f"    ✓ [缺口补齐 {key}/{name}] 补到 {len([x for x in need if x in got])}/{len(need)} 天")

    # ---- 回填 ----
    if got:
        d = d[~d["date"].dt.date.isin(set(got))]
        add = pd.DataFrame({"date": pd.to_datetime(sorted(got)), "close": [got[x] for x in sorted(got)]})
        d = pd.concat([d, add], ignore_index=True)
        for x in sorted(got):
            DATA_QUALITY["repaired"].append({"symbol": key, "date": str(x)})

    unresolved = [x for x in need if x not in got]
    if unresolved:
        have_now = set(d["date"].dt.date)
        ph = [x for x in unresolved if x not in have_now]
        if ph:
            d = pd.concat([d, pd.DataFrame({"date": pd.to_datetime(ph),
                                            "close": [float("nan")] * len(ph)})], ignore_index=True)
        for x in unresolved:
            DATA_QUALITY["unresolved"].append({"symbol": key, "date": str(x)})
        print(f"    ⚠ [缺口] {key} 未补齐 {len(unresolved)} 天：{[str(x) for x in unresolved]}"
              f" → 该因子当日不可用（上层将跳过相关预测）")

    d = d[["date", "close"]].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    return d, unresolved


def unresolved_gaps(symbols=None, since=None):
    """未修复缺口查询（供 analysis 决定是否跳过当日预测）"""
    out = []
    for g in DATA_QUALITY["unresolved"]:
        if symbols and g["symbol"] not in symbols:
            continue
        if since:
            try:
                if pd.Timestamp(g["date"]).date() < pd.Timestamp(since).date():
                    continue
            except Exception:
                pass
        out.append(g)
    return out


def index_quote(symbol):
    """指数「当日点位 + 当日涨跌幅%」——**直接取行情接口自带的涨跌幅**，
    不再用日线序列最后两根相减（序列可能因 Yahoo 空行出现缺口 → 会把多日累计当单日，
    事故：NDX 显示 -0.04%，实为 -0.85%）。
    源优先级：腾讯快照（自带涨跌幅%）→ 东财 → 新浪。返回 {date, close, pct, src} 或 None。
    """
    q = _TX_INDEX_CODE.get(symbol)
    if q:
        def _tx():
            r = requests_mod.get(TX_URL + q, headers=HEADERS_TX, timeout=15)
            r.encoding = "gbk"
            txt = (r.text or "").strip()
            if "=" not in txt:
                return None
            parts = txt.split("=", 1)[1].strip().strip('"').split("~")
            if len(parts) < 33:
                return None
            cur, pctv = float(parts[3]), float(parts[32])
            m = re.search(r"(20\d{2}-\d{2}-\d{2})", txt)
            return {"date": m.group(1) if m else "", "close": cur, "pct": pctv, "src": "腾讯"}

        out = _retry_call(_tx, attempts=2, wait=1.0, label="指数行情", verbose=False)
        if out and out.get("close"):
            return out

    for name, fn in (("东财", lambda: _em_index_series(symbol)),
                     ("新浪", lambda: _sina_series(symbol, "US"))):
        try:
            s = fn()
        except Exception as e:
            print(f"    ✗ [指数行情 {symbol}/{name}] {repr(e)[:70]}")
            continue
        if not s or len(s) < 2:
            continue
        ds = sorted(s)
        d1, d2 = ds[-1], ds[-2]
        # 相邻交易日校验：d2 必须是 d1 的前一个交易日，否则说明有缺口 → 不用（防多日累计当单日）
        cal = trading_day_set("US")
        if cal:
            prevs = sorted(x for x in cal if x < d1)
            if not prevs or prevs[-1] != d2:
                continue
        if not (s[d2] and s[d2] > 0):
            continue
        return {"date": str(d1), "close": float(s[d1]),
                "pct": (float(s[d1]) / float(s[d2]) - 1) * 100, "src": name}

    # 最后兜底：用「已补齐」的日线序列相减，但**必须通过相邻交易日校验**
    df = get_index(symbol)
    if df is not None and len(df) >= 2:
        d1 = pd.Timestamp(df["date"].iloc[-1])
        d2 = pd.Timestamp(df["date"].iloc[-2])
        lc, pc = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
        cal = trading_day_set("US")
        ok_adj = True
        if cal:
            prevs = sorted(x for x in cal if x < d1.date())
            ok_adj = bool(prevs) and prevs[-1] == d2.date()
        if ok_adj and pd.notna(lc) and pd.notna(pc) and pc > 0:
            return {"date": str(d1.date()), "close": lc, "pct": (lc / pc - 1) * 100, "src": "yf(已校验)"}
        print(f"    ⚠ [指数行情 {symbol}] 日线最后两根非相邻交易日（{d2.date()} → {d1.date()}）→ 不出涨跌幅")
    return None


def data_quality_summary():
    """报告用：缺口统计 + 明细（各最多列 20 条）"""
    def _dedup(lst):
        seen, out = set(), []
        for g in lst:
            k = (g.get("symbol"), g.get("date"))
            if k in seen:
                continue
            seen.add(k)
            out.append(g)
        return out

    found, repaired, unresolved = (_dedup(DATA_QUALITY["found"]),
                                   _dedup(DATA_QUALITY["repaired"]),
                                   _dedup(DATA_QUALITY["unresolved"]))
    return {
        "counts": {"found": len(found), "repaired": len(repaired), "unresolved": len(unresolved)},
        "repaired": repaired[:20],
        "unresolved": unresolved[:20],
    }


# ============ 收益对齐 ============

def asof_ret(prices, nav_dates, lag=0):
    """对每个净值日期 D，取美股 '交易日 <= D-lag' 的最新收盘计算当日收益

    2026-09-24 加固（缺口防线）：**不再预先 dropna 掉 NaN 收益行**。
    原因：若序列存在未修复缺口（close=NaN 占位），pct_change 会给出 NaN；
    旧实现把它 drop 后，searchsorted 会静默回退到**更早一个交易日**的收益
    → 返回一个"看起来正常但实际不对应目标日"的值（正是本次 NDX/ASML 事故的模式）。
    现在 NaN 行保留在索引里 → 命中即返回 NaN，上层据此跳过预测，**宁可无数据不给错数据**。
    """
    if prices is None or len(prices) == 0:
        return np.array([np.nan] * len(nav_dates))
    p = prices[["date", "close"]].copy()
    p["date"] = pd.to_datetime(p["date"])
    p = p.drop_duplicates("date", keep="last").sort_values("date")
    p["ret"] = p["close"].pct_change()
    s = p.set_index("date")["ret"].sort_index()
    idx = s.index
    out = []
    for d in nav_dates:
        key = pd.Timestamp(d) - pd.Timedelta(days=lag)
        pos = idx.searchsorted(key, side="right") - 1
        out.append(s.iloc[pos] if pos >= 0 else np.nan)
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
