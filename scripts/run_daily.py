#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDII 基金净值跟踪 - 每日主入口

流程（美股交易日北京时间 08:00 触发）：
1. 判断美股交易日（NYSE 日历 + 时区）——非交易日跳过
2. 目标净值日门控：预测目标净值日（= 美股最近收盘日 us_last）非 A股交易日 → 该日基金不公布
   净值，预测将永远无法验证 → 跳过（2026-09-25；此前误判「今天」是否 A股交易日）
3. 长假回归首日保护：最新净值与美股最近收盘间隔 ≥2 个美股交易日 → 跳过当日（避免多日累计大偏差）
4. 对每只基金：拉最新持仓 + 净值 + 美股行情 + 汇率
5. ⭐ 前瞻预测：用今天凌晨美股收盘数据，预测「今晚将公布」的净值涨跌（lag=0）
6. 验证历史预测：读取 predictions.jsonl，净值已公布的补记 actual，统计命中率
7. 滚动 NNLS 动态权重 → 输出疑似调仓清单
8. 生成 Markdown 报告 → output/
"""
import time as _time
_PROC_T0 = _time.time()   # 进程计时起点（2026-09-25 阶段耗时埋点；放在最前，含 import 开销）

import os, sys, json, datetime, argparse
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_fetcher as dfet
import analysis as ana

_LAST_MARK = _PROC_T0

def mark(label):
    """阶段耗时埋点（2026-09-25）：距上次埋点增量 + 自进程启动累计。
    目的：GitHub Actions 日志的 stdout 是块缓冲，时间戳被挤压成少数几个 flush 点，
    无法据此定位瓶颈 —— 用显式埋点取代（配合 workflow 的 PYTHONUNBUFFERED=1）。"""
    global _LAST_MARK
    now = _time.time()
    print(f"    ⏱ {label}: +{now - _LAST_MARK:.1f}s（累计 {now - _PROC_T0:.1f}s）", flush=True)
    _LAST_MARK = now

# 关注的 QDII 基金（A/C 份额已合并选择）
# 配置化（2026-08-16 起，2026-08-26 恢复）：优先读仓库 config/funds.json（维护面板可编辑），缺失回退内置默认
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FUNDS = ["002891", "008254", "014002", "015202", "016702", "018147", "021277", "021842",
                 "012922", "022184"]
DEFAULT_FUND_NAMES = {"002891": "华夏移动互联", "008254": "华宝致远C", "014002": "浦银全球智能C",
              "015202": "汇添富全球移动C", "016702": "银华海外数字C", "018147": "建信新兴C",
              "021277": "广发全球精选C", "021842": "国富全球科技C",
              "012922": "易方达全球C", "022184": "富国全球科技C"}

def load_funds_config():
    """从 config/funds.json 读取基金清单；文件缺失/格式错误时回退内置默认（向后兼容）。
    ⚠️ 2026-08-26 恢复：8/20 曾因本地旧代码覆盖导致此函数被回滚（config 在云端但无人消费）
    2026-09-18：新增 holdings_proxy 读取（ETF 联接基金等特殊类型 → 持仓取目标 ETF）
    返回 (funds, names, proxies)；proxies: {code: 目标ETF代码}（无代理则为空 dict）"""
    cfg_path = os.path.join(BASE_DIR, "..", "config", "funds.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        items = cfg.get("funds") or []
        funds = [str(x["code"]) for x in items if x.get("code")]
        names = {str(x["code"]): str(x.get("name", "")) for x in items if x.get("code")}
        proxies = {str(x["code"]): str(x["holdings_proxy"]) for x in items
                   if x.get("code") and x.get("holdings_proxy")}
        if funds:
            print(f"[funds] 已从 config/funds.json 加载 {len(funds)} 只基金"
                  + (f"（其中 {len(proxies)} 只走持仓代理）" if proxies else ""))
            return funds, names, proxies
        print(f"[funds] config/funds.json 为空，回退内置默认")
    except FileNotFoundError:
        print(f"[funds] 未找到 config/funds.json，回退内置默认（{len(DEFAULT_FUNDS)} 只）")
    except Exception as e:
        print(f"[funds] 读取 config/funds.json 失败（{e}），回退内置默认")
    return DEFAULT_FUNDS, DEFAULT_FUND_NAMES, {}

FUNDS, FUND_NAMES, FUND_PROXIES = load_funds_config()
OUTPUT_DIR = os.path.join(BASE_DIR, "..", "output")
HIST_FILE = os.path.join(OUTPUT_DIR, "predictions.jsonl")

def bj_now():
    BJ = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(BJ)

def is_us_trade_day_today():
    """今天是否应执行：美股最近一个交易日已收盘（北京凌晨收盘），今晚将公布对应净值。
    规则（用户验证过的对齐规则）：净值日期 D 对应美股「交易日 ≤ D」最新收盘（lag=0）。
    北京 D 日 08:00 时，美东 D-1 日已收盘 → 今晚（北京 D 日晚）公布的净值日期为 D，
    对应美股 D-1 收盘。因此判断「美东昨天（北京视角 D-1）是否美股交易日」。
    """
    BJ = datetime.timezone(datetime.timedelta(hours=8))
    now_bj = datetime.datetime.now(BJ)
    today = now_bj.strftime("%Y-%m-%d")
    us_prev = (now_bj - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=us_prev, end_date=us_prev)
        is_trade = len(sched) > 0
    except Exception:
        wd = datetime.datetime.strptime(today, "%Y-%m-%d").weekday()
        is_trade = wd < 5
    if is_trade:
        print(f"✓ {today}：美东昨日 {us_prev} 是交易日，今晚净值将更新，执行分析")
    else:
        print(f"✗ {today}：美东昨日 {us_prev} 非交易日（周末/节假日），跳过")
    return is_trade

def is_cn_nav_day(bj_date=None):
    """判断北京日期是否为 A股交易日 = QDII 基金净值更新日（2026-09-09 加入）。
    QDII 基金净值只在 A股交易日公布（基金公司按中国工作日运营）；中国长假
    （国庆/春节等，A股休市但美股照常交易）期间基金不更新净值，美股多日涨跌
    会积压到节后首个净值日一次反映。此前门控只看美股日历 → 长假期间每天
    空跑并产生永久悬挂的无效预测记录。
    """
    if bj_date is None:
        bj_date = bj_now().strftime("%Y-%m-%d")
    try:
        import pandas_market_calendars as mcal
        xshg = mcal.get_calendar("XSHG")
        sched = xshg.schedule(start_date=bj_date, end_date=bj_date)
        return len(sched) > 0
    except Exception:
        # 兜底：周末判休市，其余放行（保守不拦正常日）
        return datetime.datetime.strptime(bj_date, "%Y-%m-%d").weekday() < 5

def us_holiday_gap_days(last_date, us_last):
    """基金最新净值日 last_date 与美股最近收盘日 us_last 之间的美股交易日数。
    ≥2 说明两者间积压了多个美股交易日（长假回归首日）：今晚净值将一次反映
    多日美股累计涨跌，单日预测模型必大偏差 → 应跳过当日预测。
    普通周末/单日美股休市该值 ≤1，不拦截。返回 int；日历异常返回 0（放行）。
    """
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        start = (pd.Timestamp(last_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        sched = nyse.schedule(start_date=start, end_date=str(us_last))
        return len(sched)
    except Exception:
        return 0

def load_history():
    """读取预测历史（JSONL）"""
    rows = []
    if os.path.exists(HIST_FILE):
        with open(HIST_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows

def save_history(rows):
    os.makedirs(os.path.dirname(HIST_FILE), exist_ok=True)
    with open(HIST_FILE, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def main():
    mark("启动+import")
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略交易日判断强制运行（测试用）")
    ap.add_argument("--out", default=OUTPUT_DIR)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    now = bj_now()
    today = now.strftime("%Y-%m-%d")
    print(f"[{today} {now.strftime('%H:%M')}] QDII 净值跟踪开始")

    provisional = False   # 参考值模式（2026-09-26）：目标净值日非 A股交易日 → 该日无净值
    if not args.force:
        if not is_us_trade_day_today():
            print("今天非美股交易日（或美股未收盘），跳过自动运行")
            return 0
        print("✓ 美股交易日，执行分析")
    # 目标净值日判定（2026-09-26 由"拦截"改为"标记参考值模式"）：
    #   判据：预测目标净值日（= 美股最近收盘日 us_last）是否 A股交易日。
    #   非 A股交易日 → 该日基金不公布净值 → 预测**不对应任何真实净值**。
    #   处理：仍执行分析（用户要"美股交易就能看到反馈"），但打 provisional 标记：
    #     · 不落库（append_predictions 的防御断言自动拦截）→ 不污染命中率统计
    #     · 页面/报告标注「参考值」，休市市场按 0 计、汇率按中国日历处理
    #   2026-09-26 补：判定移到 force 分支之外 —— 参考值是「目标日」的属性，
    #     与是否强制运行无关；否则 --force 补跑休市日会得到无标注数字，容易误读。
    try:
        us_last_probe = dfet.us_last_trade_date()
    except Exception as e:
        print(f"  [目标净值日判定] 美股日历异常，按正常模式继续（{repr(e)[:60]}）")
        us_last_probe = None
    if us_last_probe is not None and not is_cn_nav_day(str(us_last_probe)):
        provisional = True
        print(f"⚠ 预测目标净值日 {us_last_probe} 非 A股交易日（QDII 基金当日不公布净值）"
              f"→ 以「参考值」模式运行：结果仅供观看，不落库、不参与验证")
    if not args.force:
        # 长假回归首日保护（2026-09-09）：基金最新净值与美股最近收盘间隔 ≥2 个美股交易日
        # → 今晚净值将一次反映多日美股累计涨跌（如国庆后 10/8 = 美股 5 天累计），
        #   单日预测必大偏差 → 跳过当天（历史验证由次日正常补跑）。
        # 参考值模式（provisional）下跳过本检查：那天本就没有对应净值，"累计偏差"不构成问题，
        #   且净值公布滞后（T+1/T+2）会让 gap 虚高（实例 9/26：最新净值 9/23 → gap=2，实际只隔 1 天）
        if not provisional:
            try:
                nav_probe = dfet.get_nav(FUNDS[0])
                if nav_probe is not None and len(nav_probe) > 0:
                    last_date = pd.Timestamp(nav_probe["date"].iloc[-1])
                    us_last = dfet.us_last_trade_date()
                    gap = us_holiday_gap_days(last_date, us_last)
                    if gap >= 2:
                        print(f"✗ 长假回归首日：基金最新净值 {last_date.date()} ↔ 美股最近收盘 "
                              f"{us_last} 间隔 {gap} 个美股交易日，今晚净值含多日累计涨跌，"
                              f"跳过当日预测（避免大偏差，历史验证次日补跑）")
                        return 0
            except Exception as e:
                print(f"  [长假检测跳过] {repr(e)[:80]}")
        else:
            try:
                nav_probe = dfet.get_nav(FUNDS[0])
                if nav_probe is not None and len(nav_probe) > 0:
                    _ld = pd.Timestamp(nav_probe["date"].iloc[-1])
                    _gap = us_holiday_gap_days(_ld, us_last_probe) if us_last_probe else 0
                    print(f"  [参考值模式] 基金最新净值 {_ld.date()} ↔ 目标日 {us_last_probe}"
                          f"（间隔 {_gap} 个美股交易日；含休市日，故本值不是单日口径的严格预测）")
            except Exception:
                pass
    mark("门控（美股日历 + 目标净值日 + 长假gap）")

    history = load_history()

    results = {}
    for code in FUNDS:
        try:
            r = ana.analyze_fund(code, holdings_proxy=FUND_PROXIES.get(code),
                                 provisional=provisional)
            results[code] = r
        except Exception as e:
            print(f"  [{code}] 失败: {repr(e)[:150]}")
            results[code] = {"code": code, "error": str(e)[:200]}
        mark(f"{code} 分析")

    # 缺口补齐结果落盘（2026-09-25）：历史交易日收盘价不可变 → 下轮直接复用（0 请求）
    try:
        _gc = dfet.gap_cache_flush()
        print(f"    [缺口缓存] 落盘 {_gc} 条", flush=True)
    except Exception as e:
        print(f"    [缺口缓存] 落盘失败: {repr(e)[:80]}")

    # 全持仓静态档案刷新（2026-09-05 加入：中报/年报披露期抓一次，供网页"全部半年报持仓"展示）
    # get_holdings_full 内部有 120 天缓存，非披露期不会重复拉取
    full_report = refresh_full_holdings()
    mark("全持仓档案刷新")

    # 验证历史预测：预测净值日已公布 → 补记 actual + 命中率
    verify_report = verify_history(history, results)
    mark("历史验证")

    # 记录今日新预测（追加到历史）
    append_predictions(history, results, today)

    # 申购限额（东财 fund_purchase_em，2026-08-18 加入；失败返回 {} 不影响主流程）
    purchase = dfet.get_fund_purchase(set(str(c) for c in results.keys()))
    mark("申购限额")

    # 30 日净值涨跌幅走势（网页对比图用：每只基金相对区间首日累计涨跌%）
    # 2026-08-21 由 60 日净值曲线改为 30 日涨跌幅，直接对比"最近30日谁涨得最好"
    trend = {}
    for code in FUNDS:
        try:
            nav = dfet.get_nav(code)
            if nav is not None and len(nav) >= 2:
                nav = nav.sort_values("date").tail(30)
                base = float(nav["nav"].iloc[0])
                if base and base > 0:
                    trend[code] = {
                        "dates": [str(d.date()) for d in nav["date"]],
                        # 相对首日累计涨跌幅 %（首日=0）
                        "pct": [round((float(v) / base - 1) * 100, 2) for v in nav["nav"]],
                    }
        except Exception:
            pass

    # NDX 当日收盘（网页右上角对照用：点位 + 当日涨跌幅；2026-08-26 加入）
    # 2026-09-24 修复：涨跌幅改为**直接取行情接口自带值**（腾讯→东财→新浪→已校验的日线），
    #   不再用"日线最后两根相减"——序列缺口会把多日累计当单日（事故：NDX -0.04% 实为 -0.85%）
    ndx_info = None
    try:
        q = dfet.index_quote(".NDX")
        if q and q.get("close"):
            ndx_info = {"date": q.get("date", ""), "close": round(float(q["close"]), 2),
                        "pct": round(float(q["pct"]), 2) if q.get("pct") is not None else None,
                        "source": q.get("src", "")}
            print(f"NDX 行情: {ndx_info['date']} close={ndx_info['close']} "
                  f"pct={ndx_info['pct']}%（源: {ndx_info['source']}）")
        else:
            print("⚠️ NDX 行情接口全部失败 → 本次不显示 NDX 涨跌幅（避免用缺口序列相减出错）")
    except Exception as e:
        print(f"⚠️ NDX 行情获取异常: {repr(e)[:120]}")
    mark("30日走势 + NDX 行情")

    # 保存当日结果
    # data_quality（2026-09-24）：价格序列缺口检测/补齐/未修复的汇总，供页面与日志暴露
    dq = dfet.data_quality_summary()
    report = {"date": today, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
              "funds": results, "verify": verify_report, "purchase": purchase,
              "trend": trend, "ndx": ndx_info, "data_quality": dq,
              "provisional": bool(provisional)}   # 参考值模式（目标净值日非 A股交易日）
    with open(os.path.join(args.out, "daily_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=_json_default)

    # 生成 Markdown 摘要
    write_summary(report, args.out, history)
    mark("报告落盘（daily_report.json + summary.md）")
    print("DONE ->", os.path.join(args.out, "daily_report.json"))
    if provisional:
        print("⚠ 本次为「参考值」模式：目标净值日非 A股交易日（该日基金不公布净值）"
              "→ 预测不落库、不参与验证统计；休市市场已按 0 计、汇率按中国日历处理")
    # 数据源使用汇总（可观测性，2026-08-21）
    print("数据源汇总:", dfet.src_summary())
    # 缺口告警汇总（2026-09-24）：防静默复发
    # ⚠️ 整块必须包 try/except：报告已写好，此处若抛异常会让整个 step 退出码非 0
    #    → workflow 的「Commit outputs」步骤被跳过 → 本次结果全丢（2026-09-24 已踩过）
    try:
        _dq_c = dq.get("counts") or {}
        print(f"数据质量: 缺口 found={_dq_c.get('found', 0)}"
              f" repaired={_dq_c.get('repaired', 0)}"
              f" from_cache={_dq_c.get('from_cache', 0)}"
              f" unresolved={_dq_c.get('unresolved', 0)}"
              f" cached_entries={dq.get('gap_cache_entries', 0)}")
        if _dq_c.get("repaired"):
            print("  ✓ 本次新补:", [g.get("symbol", "") + "@" + g.get("date", "")
                                   for g in (dq.get("repaired") or [])[:8]])
        if _dq_c.get("from_cache"):
            print(f"  ↩ 由缓存补齐（0 请求）: {_dq_c['from_cache']} 处")
        if _dq_c.get("unresolved"):
            print("  ⛔ 未修复（相关基金当日已跳过预测）:",
                  [g.get("symbol", "") + "@" + g.get("date", "")
                   for g in (dq.get("unresolved") or [])[:8]])
        _blocked = [k for k, v in (results or {}).items()
                    if isinstance(v, dict) and v.get("predict_blocked")]
        if _blocked:
            print(f"⛔ 因数据缺口跳过当日预测的基金（{len(_blocked)}/{len(results)}）:", _blocked)
    except Exception as e:
        print(f"⚠️ 数据质量汇总打印失败（不影响本次结果）: {repr(e)[:150]}")
    mark("总计")
    return 0

def _json_default(o):
    """json 序列化兜底：numpy 类型转原生（bool 保持布尔，不转字符串）
    ⚠️ 曾用 default=str 导致 np.bool_ 序列化成字符串 "False"，
       render_html 里 bool("False")==True → 8 个基金全误判"与大盘背离"（2026-08-13）
    ⚠️ 2026-08-20 曾误插进 main() 内部导致 write_summary 永不执行，已移到模块级"""
    import numpy as _np
    if isinstance(o, (_np.bool_,)):
        return bool(o)
    if isinstance(o, (_np.integer,)):
        return int(o)
    if isinstance(o, (_np.floating,)):
        return float(o)
    if isinstance(o, (_np.ndarray,)):
        return o.tolist()
    return str(o)

FULL_REPORT_PATH = os.path.join(OUTPUT_DIR, "holdings_full.json")

def refresh_full_holdings(force=False):
    """刷新全部半年报持仓静态档案（2026-09-05）
    逐基金调 get_holdings_full（内部 120 天缓存，披露期才真拉），汇总写 output/holdings_full.json
    返回 dict：{"report_date": "...", "funds": {code: {...}}}
    """
    report_date = "2026-06-30"  # 当前中报截止日（Q2 2026）；年报期需更新
    funds_out = {}
    any_live = False
    for code in FUNDS:
        try:
            # holdings_proxy（2026-09-18）：ETF 联接基金全持仓取自目标 ETF（穿透披露）
            src_code = FUND_PROXIES.get(code, code)
            h, src = dfet.get_holdings_full(src_code, force=force)
            if not h:
                continue
            # 2026-09-05 修复：get_holdings_full 内部会更新缓存文件，
            # 需重新读取才能拿到最新 ts（原实现在循环前快照一次 → 大部分基金 ts 为空）
            entry = (dfet._load_full_cache().get(src_code)) or {}
            funds_out[code] = {"holdings": h, "ts": entry.get("ts", ""),
                               "count": len(h), "source": src,
                               "holdings_proxy": src_code if src_code != code else None}
            if src == "live":
                any_live = True
        except Exception as e:
            print(f"  [全持仓 {code}] 失败: {repr(e)[:100]}")
    if not funds_out:
        print("  [全持仓] 无任何基金全持仓数据")
        return None
    report = {"report_date": report_date, "funds": funds_out,
              "refreshed_at": bj_now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(FULL_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=_json_default)
        print(f"  [全持仓] 已写 {FULL_REPORT_PATH}（{len(funds_out)} 只基金，live={any_live}）")
    except Exception as e:
        print(f"  [全持仓] 写文件失败: {repr(e)[:100]}")
    return report

def append_predictions(history, results, today):
    """把今日预测写入历史 JSONL（按 (code, pred_date) 去重 + 覆盖更新）
    - 已存在 (code, pred_date) → 覆盖更新为最新预测（保留 actual 等验证字段）
    - 不存在 → 追加新记录
    """
    for code, r in results.items():
        if "error" in r or "predict" not in r or r["predict"] is None:
            continue
        p = r["predict"]
        pred_date = str(p["next_date"].date())
        # 防御断言（2026-09-25）：目标净值日必须是 A股交易日 —— 否则永远不会有对应净值，
        # 落库即成「等不到 actual」的悬挂记录。即便门控将来再被改坏，也不会再产生这类记录。
        if not is_cn_nav_day(pred_date):
            print(f"  ⛔ [防御] {code} pred_date={pred_date} 非 A股交易日 → 不落预测记录")
            continue
        # 清理同 (code, pred_date) 的重复旧记录（只保留第一条）
        dups = [i for i, h in enumerate(history)
                if h.get("code") == code and h.get("pred_date") == pred_date]
        if dups:
            # 覆盖更新第一条，删除其余重复
            idx = dups[0]
            old = history[idx]
            for j in sorted(dups[1:], reverse=True):
                history.pop(j)
            history[idx] = {
                "code": code,
                "name": FUND_NAMES.get(code, ""),
                "run_date": today,
                "pred_date": pred_date,
                "last_nav": p["last_nav"],
                "pred_static": p["pred_static"],
                "pred_nnls": p["pred_nnls"],
                "pred_nav_static": p["pred_nav_static"],
                "actual": old.get("actual"),   # 保留已验证结果
                "actual_nav": old.get("actual_nav"),
                "hit": old.get("hit"),
                "err": old.get("err"),
            }
        else:
            history.append({
                "code": code,
                "name": FUND_NAMES.get(code, ""),
                "run_date": today,
                "pred_date": pred_date,
                "last_nav": p["last_nav"],
                "pred_static": p["pred_static"],
                "pred_nnls": p["pred_nnls"],
                "pred_nav_static": p["pred_nav_static"],
                "actual": None,       # 待净值公布后回填
                "actual_nav": None,
                "hit": None,          # 方向是否命中
                "err": None,          # 静态预测误差
            })
    save_history(history)

def verify_history(history, results):
    """验证历史预测：预测净值日已公布的，补记 actual 并统计命中率
    自动收敛：验证前清理同 (code, pred_date) 的重复记录（保留第一条/已验证的）

    精度评估指标（2026-08-20 升级）：
      - dir_acc    方向命中率：sign(预测)==sign(实际)
      - mag_acc    幅度命中率：|误差| ≤ 1.0pp（1个百分点）
      - combo_acc  综合精度命中：方向对 且 |误差| ≤ 1.0pp
      - cover      区间覆盖率：实际 ∈ 预测 ± 1.28×MAE（80%置信带）
      - mae / rmse / bias
    """
    # ---- 自动收敛重复 ----
    seen = set()
    keep_idx = []
    for i, h in enumerate(history):
        key = (h.get("code"), h.get("pred_date"))
        if key in seen:
            continue
        seen.add(key)
        keep_idx.append(i)
    if len(keep_idx) != len(history):
        history[:] = [history[i] for i in keep_idx]

    stats = {"n": 0, "dir_hit": 0, "mae_sum": 0.0, "mag_hit": 0, "combo_hit": 0,
             "cover_hit": 0, "bias_sum": 0.0, "sq_sum": 0.0}
    for h in history:
        if h.get("actual") is not None:
            stats["n"] += 1
            stats["dir_hit"] += 1 if h.get("hit") else 0
            if h.get("err") is not None:
                e = h["err"]
                stats["mae_sum"] += abs(e)
                stats["bias_sum"] += e
                stats["sq_sum"] += e ** 2
                if abs(e) <= 0.010:  # ±1.0pp
                    stats["mag_hit"] += 1
                if h.get("hit") and abs(e) <= 0.010:
                    stats["combo_hit"] += 1
            continue
        code = h["code"]
        pred_date = h["pred_date"]
        r = results.get(code)
        if r is None or "error" in r:
            continue
        # 检查该预测净值日是否已公布（净值数据已含该日）
        nav = dfet.get_nav(code)
        if nav is None or len(nav) == 0:
            continue
        nav = nav[nav["date"] <= pd.Timestamp(pred_date)]
        if len(nav) == 0:
            continue
        row = nav.iloc[-1]
        if str(row["date"].date()) != pred_date:
            continue  # 尚未公布
        actual = row["growth"] / 100
        h["actual"] = actual
        h["actual_nav"] = float(row["nav"])
        h["hit"] = bool(np.sign(h["pred_static"]) == np.sign(actual))
        h["err"] = h["pred_static"] - actual
        stats["n"] += 1
        stats["dir_hit"] += 1 if h["hit"] else 0
        e = h["err"]
        stats["mae_sum"] += abs(e)
        stats["bias_sum"] += e
        stats["sq_sum"] += e ** 2
        if abs(e) <= 0.010:
            stats["mag_hit"] += 1
        if h["hit"] and abs(e) <= 0.010:
            stats["combo_hit"] += 1
    save_history(history)
    if stats["n"] > 0:
        n = stats["n"]
        stats["dir_acc"] = stats["dir_hit"] / n * 100
        stats["mae"] = stats["mae_sum"] / n * 100
        stats["mag_acc"] = stats["mag_hit"] / n * 100       # 幅度命中率 ±1pp
        stats["combo_acc"] = stats["combo_hit"] / n * 100   # 综合精度命中
        stats["rmse"] = (stats["sq_sum"] / n) ** 0.5 * 100
        stats["bias"] = stats["bias_sum"] / n * 100         # 平均误差（pp，正=高估）
        # 区间覆盖率：实际 ∈ 预测 ± 1.28×MAE（80%置信带，正态近似）
        band = stats["mae"] * 1.28
        cover = 0
        for h in history:
            if h.get("actual") is not None and h.get("err") is not None:
                if abs(h["err"]) <= band / 100:
                    cover += 1
        stats["cover"] = cover / n * 100
        stats["band"] = band  # 误差带宽度（pp）
    else:
        stats["dir_acc"], stats["mae"] = None, None
        stats["mag_acc"] = stats["combo_acc"] = stats["rmse"] = stats["bias"] = stats["cover"] = None
        stats["band"] = None
    # 最近 5 条已验证记录
    verified = [h for h in history if h.get("actual") is not None][-5:]
    stats["recent"] = [{"code": h["code"], "pred_date": h["pred_date"],
                        "pred_static": h["pred_static"], "actual": h["actual"],
                        "hit": h["hit"], "err": h["err"]} for h in verified]
    return stats

def write_summary(report, out_dir, history=None):
    """生成对比摘要 Markdown"""
    history = history or []
    _prov = bool(report.get("provisional"))
    lines = [f"# QDII 净值跟踪日报（{report['date']}）", "",
             f"> 生成：{report['generated_at']} ｜ 数据：天天基金F10 + akshare ｜ 方法：二十大持仓静态 + 滚动NNLS动态",
             ""]
    if _prov:
        lines += ["> ⚠️ **参考值模式**：目标净值日非 A股交易日（QDII 基金当日不公布净值）→ "
                  "下列预测**不对应任何真实净值**，仅供观看参考，**不落库、不参与验证统计**。"
                  "休市市场当日收益按 0 计（非缺失剔除）、汇率按中国日历处理。", ""]

    # ⭐ 核心板块：今晚净值预测（保留全部基金：待验证显示预测，已公布显示预测vs实际）
    lines.append("## ⭐ 今晚净值预测（今日凌晨美股收盘 → 对应净值日）" + ("（参考值）" if _prov else ""))
    lines.append("")
    lines.append("| 代码 | 基金 | 预测净值日 | 静态预测 | 滚动NNLS | 预测净值 | 最新净值 | 状态 |")
    lines.append("|------|------|:---:|:---:|:---:|:---:|:---:|:---:|")
    for code, r in report["funds"].items():
        name = FUND_NAMES.get(code, "")
        if "error" in r:
            lines.append(f"| {code} | {name} | - | 错误 | | | | |")
            continue
        p = r.get("predict")
        if p is not None:
            pred_date = str(p["next_date"].date())
            pn = p.get("pred_nnls")
            pn_s = f"{pn*100:+.2f}%" if pn is not None else "-"
            # 方向背离提示 + 持仓缓存标注
            diverge_note = " ⚠️与大盘背离" if p.get("diverge") else ""
            src_note = ""
            hs = r.get("holdings_source") or {}
            if hs.get("q2") == "cache":
                src_note = " (持仓缓存)"
            lines.append(f"| {code} | {name} | **{pred_date}** | **{p['pred_static']*100:+.2f}%** | {pn_s} | "
                         f"**{p['pred_nav_static']:.4f}** | {p['last_nav']:.4f} | 待公布{diverge_note}{src_note} |")
        else:
            # 已公布：从 history 找最新已验证记录显示预测 vs 实际
            recs = [h for h in history if h.get("code") == code and h.get("actual") is not None]
            if recs:
                rec = recs[-1]
                # 综合命中：方向对 且 |误差|≤1.0pp 才算 ✓
                hit = "✓" if (rec.get("hit") and abs(rec.get("err") or 0) <= 0.010) else "✗"
                lines.append(f"| {code} | {name} | {rec['pred_date']} | **{rec['pred_static']*100:+.2f}%** | "
                             f"{rec.get('pred_nnls')*100 if rec.get('pred_nnls') is not None else 0:+.2f}% | "
                             f"{rec.get('pred_nav_static', 0):.4f} | {rec.get('actual_nav', 0):.4f} | "
                             f"已公布 实际{rec['actual']*100:+.2f}% {hit} |")
            else:
                lines.append(f"| {code} | {name} | - | - | | | | 无记录 |")
    lines.append("")
    lines.append("> 规则：净值日期 D 对应美股「交易日 ≤ D」最新收盘（lag=0）。预测对象=美股最近收盘日，各基金统一；已公布净值的基金显示预测 vs 实际对照。")
    lines.append("")

    # 历史预测验证
    v = report.get("verify") or {}
    if v.get("n"):
        lines.append(f"## 历史预测验证（已验证 {v['n']} 条）")
        lines.append("")
        lines.append(f"**方向命中率 {v['dir_acc']:.1f}% ｜ 幅度命中率±1pp {v.get('mag_acc', 0):.1f}% ｜ "
                     f"综合精度 {v.get('combo_acc', 0):.1f}% ｜ 区间覆盖率 {v.get('cover', 0):.0f}% ｜ "
                     f"MAE {v['mae']:.3f}pp ｜ bias {v.get('bias', 0):+.2f}pp**")
        lines.append("")
        lines.append("> 闭环：早上预测 → 当晚净值公布 → 次日早上自动验证回填。"
                     f"幅度命中=|误差|≤1.0pp；区间=实际落在预测±{v.get('band', 0):.2f}pp误差带内（80%置信）。")
        lines.append("")
        lines.append("| 代码 | 预测来源日 | 预测净值日 | 预测涨跌 | 实际涨跌 | 命中 | 误差 |")
        lines.append("|------|---------|---------|:---:|:---:|:---:|:---:|")
        # 用 history 补 run_date
        hist_by_key = {(h.get("code"), h.get("pred_date")): h for h in history}
        for rc in (v.get("recent") or [])[::-1]:
            # 综合命中：方向对 且 |误差|≤1.0pp 才算 ✓
            hit = "✓" if (rc["hit"] and abs(rc.get("err") or 0) <= 0.010) else "✗"
            hk = hist_by_key.get((rc["code"], rc["pred_date"]), {})
            run_date = str(hk.get("run_date", ""))[:10]
            lines.append(f"| {rc['code']} | {run_date} | {rc['pred_date']} | {rc['pred_static']*100:+.2f}% | "
                         f"{rc['actual']*100:+.2f}% | {hit} | {rc['err']*100:+.2f}pp |")
        lines.append("")

    # 持仓真实性 + 美股含量
    lines.append("## 持仓真实性 + 美股含量")
    lines.append("")
    lines.append("| 代码 | 基金 | 披露美股% | NDXβ | 静态预测方向% | 静态MAE | 滚动MAE | 持仓R²(Q2) |")
    lines.append("|------|------|:---:|:---:|:---:|:---:|:---:|:---:|")
    for code, r in report["funds"].items():
        name = FUND_NAMES.get(code, "")
        if "error" in r:
            lines.append(f"| {code} | {name} | 错误 | | | | | |")
            continue
        us = r.get("us_pct", "-")
        beta = f"{r['ndx_beta']['ndx_beta']:.2f}" if r.get("ndx_beta") else "-"
        s = r.get("static") or {}
        rr = r.get("roll") or {}
        r2 = s.get("r2", "-")
        # 2026-08-26 容错：static 可能缺失/None（如分析失败），不能直接 :.1f 格式化
        _da = s.get("dir_acc")
        _mae = s.get("mae")
        _rmae = rr.get("mae")
        _da_s = f"{_da:.1f}" if isinstance(_da, (int, float)) else "-"
        _mae_s = f"{_mae:.2f}" if isinstance(_mae, (int, float)) else "-"
        _rmae_s = f"{_rmae:.2f}" if isinstance(_rmae, (int, float)) else "-"
        _r2_s = f"{r2:.3f}" if isinstance(r2, (int, float)) else "-"
        lines.append(f"| {code} | {name} | {us}% | {beta} | {_da_s}% | "
                     f"{_mae_s} | {_rmae_s} | {_r2_s} |")
    lines.append("")

    # 预测明细（每只基金持仓股贡献）
    lines.append("## 今晚预测明细（静态披露权重 × 最新美股收益）")
    lines.append("")
    for code, r in report["funds"].items():
        if "error" in r or "predict" not in r or r["predict"] is None:
            continue
        p = r["predict"]
        lines.append(f"### {code} {FUND_NAMES.get(code, '')} → 预测 {p['pred_static']*100:+.2f}%")
        lines.append("")
        lines.append("| 代码 | 权重 | 最新收益 | 贡献 |")
        lines.append("|------|:---:|:---:|:---:|")
        for c in p["contributors"]:
            lines.append(f"| {c['code']} | {c['weight']*100:.2f}% | {c['ret']*100:+.2f}% | {c['contrib']*100:+.3f}pp |")
        lines.append(f"| USDCNH | - | {p['fx_ret']*100 if p['fx_ret'] is not None else 0.0:+.2f}% | "
                     f"{'已计入' if p['fx_ret'] is not None else '源不可用'} |")
        lines.append("")
    lines.append("## 疑似调仓（滚动NNLS vs 披露 · 全部二十大持仓）")
    lines.append("")
    for code, r in report["funds"].items():
        if "error" in r:
            continue
        lines.append(f"### {code} {FUND_NAMES.get(code, '')}")
        lines.append("")
        lines.append("| 代码 | 名称 | 披露% | NNLS估计% | 差异 |")
        lines.append("|------|------|:---:|:---:|:---:|")
        h_q2 = r.get("holdings", [])
        disc = {x["code"]: x["pct"] for x in h_q2}
        names = {x["code"]: x["name"] for x in h_q2}
        nnls = r.get("nnls_weight") or {}
        # 按披露权重降序展示全部二十大持仓（NNLS 未估计到的显示 0）
        for x in sorted(h_q2, key=lambda v: v["pct"], reverse=True):
            c = x["code"]
            d = x["pct"]
            w = nnls.get(c, 0)
            if w is None:
                w = 0
            diff = w * 100 - d
            flag = "▲加仓" if diff > 2 else ("▼减仓" if diff < -2 else "")
            lines.append(f"| {c} | {names.get(c, '-')} | {d:.1f}% | {w*100:.1f}% | {diff:+.1f}% {flag} |")
        lines.append("")
    path = os.path.join(out_dir, "summary.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("SUMMARY ->", path)

if __name__ == "__main__":
    sys.exit(main())
