#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDII 基金净值跟踪 - 每日主入口

流程（美股交易日北京时间 08:00 触发）：
1. 判断美股交易日（NYX 日历 + 时区）——非交易日跳过
2. 对每只基金：拉最新持仓 + 净值 + 美股行情 + 汇率
3. 预测"今晚将公布"的净值涨跌（用昨日美股收盘，lag=0）
4. 验证"昨晚已公布"的净值涨跌（与历史预测对比）
5. 滚动 NNLS 动态权重 → 输出疑似调仓清单
6. 生成 Markdown 报告 → output/
"""
import os, sys, json, datetime, argparse
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_fetcher as dfet
import analysis as ana

# 关注的 QDII 基金（A/C 份额已合并选择）
FUNDS = ["002891", "008254", "014002", "015202", "016702", "018147", "021277", "021842"]
FUND_NAMES = {"002891": "华夏移动互联", "008254": "华宝致远C", "014002": "浦银全球智能C",
              "015202": "汇添富全球移动C", "016702": "银华海外数字C", "018147": "建信新兴C",
              "021277": "广发全球精选C", "021842": "国富全球科技C"}
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")

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
    # 美东昨天 = 北京今天 - 1 天（美东收盘在北京次日凌晨）
    us_prev = (now_bj - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=us_prev, end_date=us_prev)
        is_trade = len(sched) > 0
    except Exception:
        # 兜底：周一到周五近似（无法识别节假日，但不至于静默失败）
        wd = datetime.datetime.strptime(today, "%Y-%m-%d").weekday()
        is_trade = wd < 5
    if is_trade:
        print(f"✓ {today}：美东昨日 {us_prev} 是交易日，今晚净值将更新，执行分析")
    else:
        print(f"✗ {today}：美东昨日 {us_prev} 非交易日（周末/节假日），跳过")
    return is_trade

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略交易日判断强制运行（测试用）")
    ap.add_argument("--out", default=OUTPUT_DIR)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    now = bj_now()
    today = now.strftime("%Y-%m-%d")
    print(f"[{today} {now.strftime('%H:%M')}] QDII 净值跟踪开始")

    if not args.force:
        if not is_us_trade_day_today():
            print("今天非美股交易日（或美股未收盘），跳过自动运行")
            return 0
        print("✓ 美股交易日，执行分析")

    results = {}
    for code in FUNDS:
        try:
            r = ana.analyze_fund(code)
            results[code] = r
        except Exception as e:
            print(f"  [{code}] 失败: {repr(e)[:150]}")
            results[code] = {"code": code, "error": str(e)[:200]}

    # 保存当日结果
    report = {"date": today, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
              "funds": results}
    with open(os.path.join(args.out, "daily_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)

    # 生成 Markdown 摘要
    write_summary(report, args.out)
    print("DONE ->", os.path.join(args.out, "daily_report.json"))
    return 0

def write_summary(report, out_dir):
    """生成对比摘要 Markdown"""
    lines = [f"# QDII 净值跟踪日报（{report['date']}）", "",
             f"> 生成：{report['generated_at']} ｜ 数据：天天基金F10 + akshare ｜ 方法：十大持仓静态 + 滚动NNLS动态",
             ""]
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
        lines.append(f"| {code} | {name} | {us}% | {beta} | {s.get('dir_acc','-'):.1f}% | "
                     f"{s.get('mae','-'):.2f} | {rr.get('mae','-'):.2f} | {r2:.3f} |")
    lines.append("")
    lines.append("## 疑似调仓（滚动NNLS vs 披露）")
    lines.append("")
    for code, r in report["funds"].items():
        if "error" in r or not r.get("nnls_weight"):
            continue
        lines.append(f"### {code} {FUND_NAMES.get(code, '')}")
        lines.append("")
        lines.append("| 代码 | 披露% | NNLS估计% | 差异 |")
        lines.append("|------|:---:|:---:|:---:|")
        h_q2 = r.get("holdings", [])
        disc = {x["code"]: x["pct"] for x in h_q2}
        for c, w in sorted(r["nnls_weight"].items(), key=lambda kv: kv[1], reverse=True):
            if c == "FX":
                continue
            d = disc.get(c, 0)
            diff = w * 100 - d
            flag = "▲加仓" if diff > 2 else ("▼减仓" if diff < -2 else "")
            lines.append(f"| {c} | {d:.1f}% | {w*100:.1f}% | {diff:+.1f}% {flag} |")
        lines.append("")
    path = os.path.join(out_dir, "summary.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("SUMMARY ->", path)

if __name__ == "__main__":
    sys.exit(main())
