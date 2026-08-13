#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDII 基金净值跟踪 - 每日主入口

流程（美股交易日北京时间 08:00 触发）：
1. 判断美股交易日（NYSE 日历 + 时区）——非交易日跳过
2. 对每只基金：拉最新持仓 + 净值 + 美股行情 + 汇率
3. ⭐ 前瞻预测：用今天凌晨美股收盘数据，预测「今晚将公布」的净值涨跌（lag=0）
4. 验证历史预测：读取 predictions.jsonl，净值已公布的补记 actual，统计命中率
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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

    history = load_history()

    results = {}
    for code in FUNDS:
        try:
            r = ana.analyze_fund(code)
            results[code] = r
        except Exception as e:
            print(f"  [{code}] 失败: {repr(e)[:150]}")
            results[code] = {"code": code, "error": str(e)[:200]}

    # 验证历史预测：预测净值日已公布 → 补记 actual + 命中率
    verify_report = verify_history(history, results)

    # 记录今日新预测（追加到历史）
    append_predictions(history, results, today)

    # 保存当日结果
    report = {"date": today, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
              "funds": results, "verify": verify_report}
    with open(os.path.join(args.out, "daily_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=_json_default)

def _json_default(o):
    """json 序列化兜底：numpy 类型转原生（bool 保持布尔，不转字符串）
    ⚠️ 曾用 default=str 导致 np.bool_ 序列化成字符串 "False"，
       render_html 里 bool("False")==True → 8 个基金全误判"与大盘背离"（2026-08-13）"""
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

    # 生成 Markdown 摘要
    write_summary(report, args.out, history)
    print("DONE ->", os.path.join(args.out, "daily_report.json"))
    return 0

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

    stats = {"n": 0, "dir_hit": 0, "mae_sum": 0.0}
    for h in history:
        if h.get("actual") is not None:
            stats["n"] += 1
            stats["dir_hit"] += 1 if h.get("hit") else 0
            if h.get("err") is not None:
                stats["mae_sum"] += abs(h["err"])
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
        stats["mae_sum"] += abs(h["err"])
    save_history(history)
    if stats["n"] > 0:
        stats["dir_acc"] = stats["dir_hit"] / stats["n"] * 100
        stats["mae"] = stats["mae_sum"] / stats["n"] * 100
    else:
        stats["dir_acc"], stats["mae"] = None, None
    # 最近 5 条已验证记录
    verified = [h for h in history if h.get("actual") is not None][-5:]
    stats["recent"] = [{"code": h["code"], "pred_date": h["pred_date"],
                        "pred_static": h["pred_static"], "actual": h["actual"],
                        "hit": h["hit"], "err": h["err"]} for h in verified]
    return stats

def write_summary(report, out_dir, history=None):
    """生成对比摘要 Markdown"""
    history = history or []
    lines = [f"# QDII 净值跟踪日报（{report['date']}）", "",
             f"> 生成：{report['generated_at']} ｜ 数据：天天基金F10 + akshare ｜ 方法：十大持仓静态 + 滚动NNLS动态",
             ""]

    # ⭐ 核心板块：今晚净值预测（保留全部基金：待验证显示预测，已公布显示预测vs实际）
    lines.append("## ⭐ 今晚净值预测（今日凌晨美股收盘 → 对应净值日）")
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
                hit = "✓" if rec.get("hit") else "✗"
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
        lines.append(f"## 历史预测验证（已公布 {v['n']} 条）")
        lines.append("")
        lines.append(f"**方向命中率 {v['dir_acc']:.1f}% ｜ MAE {v['mae']:.3f}pp**")
        lines.append("")
        lines.append("| 代码 | 预测日期 | 预测涨跌 | 实际涨跌 | 命中 | 误差 |")
        lines.append("|------|---------|:---:|:---:|:---:|:---:|")
        for rc in (v.get("recent") or [])[::-1]:
            hit = "✓" if rc["hit"] else "✗"
            lines.append(f"| {rc['code']} | {rc['pred_date']} | {rc['pred_static']*100:+.2f}% | "
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
        lines.append(f"| {code} | {name} | {us}% | {beta} | {s.get('dir_acc','-'):.1f}% | "
                     f"{s.get('mae','-'):.2f} | {rr.get('mae','-'):.2f} | {r2:.3f} |")
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
    lines.append("## 疑似调仓（滚动NNLS vs 披露 · 全部十大持仓）")
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
        # 按披露权重降序展示全部十大持仓（NNLS 未估计到的显示 0）
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
