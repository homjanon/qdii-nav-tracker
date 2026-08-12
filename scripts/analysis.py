#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDII 基金净值跟踪 - 核心分析模块

功能：
1. 静态披露权重预测：用当期十大持仓 × 美股日收益（lag=0 对齐）+ USDCNH 折算
2. 滚动 NNLS 动态权重：walk-forward 重估十大权重，追踪调仓，提升幅度预测精度
3. 披露真实性验证：披露权重回测 R² / 方向一致率（与历史对比）
4. 美股含量评估：披露美股占比 + 指数回归 NDX β
"""
import numpy as np
import pandas as pd
from scipy.optimize import nnls
import data_fetcher as dfet

US_CODES_POOL = None  # 动态识别，不硬编码

def _weights(holdings):
    """披露权重 {code: pct/100}，仅保留有行情源的美股/港股/A股"""
    w = {}
    for h in holdings:
        if h["market"] in ("US", "HK", "CN"):
            w[h["code"]] = h["pct"] / 100
    return w

def basket_returns(nav, holdings, price_map, fx_df, lag=0):
    """披露权重篮子收益（对齐净值日期）"""
    w = _weights(holdings)
    wsum = sum(w.values())
    total = np.full(len(nav), np.nan)
    for i, d in enumerate(nav["date"]):
        b = 0.0
        ok = True
        for code, wgt in w.items():
            px = price_map.get(code)
            if px is None:
                continue
            r = dfet.asof_ret(px, [d], lag)[0]
            if np.isnan(r):
                ok = False
                break
            b += wgt * r
        if not ok:
            continue
        fxr = np.nan
        if fx_df is not None:
            fxr = dfet.asof_ret(fx_df, [d], lag)[0]
            if not np.isnan(fxr):
                b += wsum * fxr
        total[i] = b
    return total

def evaluate(nav, pred):
    """评估预测 vs 实际"""
    v = pd.DataFrame({"date": nav["date"], "actual": nav["growth"] / 100, "pred": pred}).dropna()
    if len(v) < 10:
        return None
    v["err"] = v["pred"] - v["actual"]
    v["dir_ok"] = np.sign(v["pred"]) == np.sign(v["actual"])
    return {"n": len(v),
            "dir_acc": (v["dir_ok"].mean() * 100),
            "mae": v["err"].abs().mean() * 100,
            "corr": v["pred"].corr(v["actual"]),
            "r2": v["pred"].corr(v["actual"]) ** 2,
            "bias": v["err"].mean() * 100}

def rolling_nnls(nav, holdings, price_map, fx_df, window=60, min_n=30):
    """滚动 NNLS 动态权重：对每个净值日 t，用 [t-window, t-1] 估计权重，预测 t
    返回: preds Series + 最新权重 dict"""
    w0 = _weights(holdings)
    codes = [c for c in w0 if c in price_map]
    if not codes:
        return None, None, None
    fx_codes = codes + ["FX"]
    # 构建收益矩阵
    X = pd.DataFrame({"date": nav["date"]})
    for c in codes:
        X[c] = dfet.asof_ret(price_map[c], nav["date"])
    if fx_df is not None:
        X["FX"] = dfet.asof_ret(fx_df, nav["date"])
    else:
        X["FX"] = 0.0
    X = X.set_index("date")
    Y = nav.set_index("date")["growth"] / 100
    full = pd.concat([Y, X], axis=1).dropna()
    if len(full) <= window:
        return None, None, None

    dates = full.index
    preds = {}
    last_w = None
    for i in range(window, len(dates)):
        hist = full.iloc[i - window:i]
        yh = hist.iloc[:, 0].values
        Xh = hist.iloc[:, 1:].values
        col_mask = [True] * (len(codes) + 1)  # codes + FX
        try:
            w_nn, _ = nnls(Xh[:, col_mask], yh)
        except Exception:
            continue
        x_today = X.loc[dates[i]].values[col_mask]
        if np.any(np.isnan(x_today)):
            continue
        preds[dates[i]] = float(w_nn @ x_today)
        last_w = dict(zip(fx_codes, w_nn))
    if not preds:
        return None, None, None
    ps = pd.Series(preds)
    ps.index.name = "date"
    return ps, last_w, full

def index_beta(nav, ndx_df, start_date=None):
    """净值对 NDX 的暴露（近6月 OLS 回归）"""
    if ndx_df is None:
        return None
    d = nav.copy()
    if start_date:
        d = d[d["date"] >= pd.Timestamp(start_date)]
    d = d.set_index("date")
    y = d["growth"] / 100
    ndx = ndx_df.copy()
    ndx["ret"] = ndx["close"].pct_change()
    ndx = ndx.dropna(subset=["ret"]).set_index("date")["ret"]
    ndx = ndx[~ndx.index.duplicated(keep="last")]
    X = ndx.reindex(d.index).ffill()
    m = pd.concat([y, X], axis=1).dropna()
    if len(m) < 30:
        return None
    yv = m.iloc[:, 0].values
    xv = np.column_stack([np.ones(len(m)), m.iloc[:, 1].values])
    beta, *_ = np.linalg.lstsq(xv, yv, rcond=None)
    yhat = xv @ beta
    r2 = 1 - ((yv - yhat) ** 2).sum() / ((yv - yv.mean()) ** 2).sum()
    return {"ndx_beta": float(beta[1]), "r2": float(r2), "alpha": float(beta[0]), "n": len(m)}

def next_nav_date(nav):
    """下一个净值日：最新净值日期的下一个工作日（跳过周末）"""
    last = nav["date"].iloc[-1]
    nd = last + pd.Timedelta(days=1)
    while nd.weekday() >= 5:
        nd += pd.Timedelta(days=1)
    return nd

def predict_next(nav, holdings, price_map, fx_df, nnls_weight=None, mae_static=None,
                 us_last=None, ndx_df=None):
    """前瞻预测：用美股最近收盘（统一基准 us_last）预测对应净值日涨跌

    时间对齐（用户验证过的规则）：净值日期 D 对应美股「交易日 ≤ D」最新收盘（lag=0）。
    us_last = 美股最近一个已收盘交易日（所有基金统一）。
    分流逻辑：
      - 该基金最新净值日期 < us_last → 该期净值未公布 → 生成预测（待验证）
      - 该基金最新净值日期 >= us_last → 该期已公布 → 返回 None（由 verify 分支验证）
    方向背离检测：预测方向与 NDX 指数方向相反 → 标注 diverge=True（提示谨慎）

    返回 dict 或 None（已公布时）
    """
    if us_last is None:
        us_last = dfet.us_last_trade_date()
    last_row = nav.iloc[-1]
    last_date = last_row["date"]
    last_nav = float(last_row["nav"])

    # 分流：若该期净值已公布（最新净值日期 >= us_last），不再预测
    if last_date.date() >= us_last:
        print(f"    净值已更新至 {last_date.date()} ≥ US基准 {us_last}，该期已公布，走验证")
        return None

    next_d = pd.Timestamp(us_last)
    # 静态披露权重预测
    w = _weights(holdings)
    wsum = sum(w.values())
    b_static = 0.0
    contributors = []
    for code, wgt in w.items():
        px = price_map.get(code)
        if px is None:
            continue
        r = dfet.asof_ret(px, [next_d])[0]
        if np.isnan(r):
            continue
        b_static += wgt * r
        contributors.append({"code": code, "weight": wgt, "ret": r, "contrib": wgt * r})
    fxr = np.nan
    if fx_df is not None:
        fxr = dfet.asof_ret(fx_df, [next_d])[0]
        if not np.isnan(fxr):
            b_static += wsum * fxr
    contributors.sort(key=lambda x: x["contrib"], reverse=True)

    # 方向背离检测：NDX 当日收益与预测方向相反 → 提示谨慎
    ndx_ret = np.nan
    diverge = False
    if ndx_df is not None:
        ndx_ret = dfet.asof_ret(ndx_df, [next_d])[0]
        if not np.isnan(ndx_ret) and not np.isnan(b_static):
            diverge = np.sign(b_static) != np.sign(ndx_ret)

    # 滚动 NNLS 权重预测
    b_nnls = None
    if nnls_weight:
        b_nnls = 0.0
        for code, wgt in nnls_weight.items():
            if code == "FX":
                continue
            px = price_map.get(code)
            if px is None:
                continue
            r = dfet.asof_ret(px, [next_d])[0]
            if np.isnan(r):
                continue
            b_nnls += wgt * r
        fxw = nnls_weight.get("FX", 0.0)
        if fx_df is not None and not np.isnan(fxr):
            b_nnls += fxw * fxr

    out = {"next_date": next_d, "last_date": last_date, "last_nav": last_nav,
           "pred_static": float(b_static), "pred_nnls": float(b_nnls) if b_nnls is not None else None,
           "pred_nav_static": float(last_nav * (1 + b_static)),
           "pred_nav_nnls": float(last_nav * (1 + b_nnls)) if b_nnls is not None else None,
           "contributors": contributors, "fx_ret": float(fxr) if not np.isnan(fxr) else None,
           "us_last": us_last, "ndx_ret": float(ndx_ret) if not np.isnan(ndx_ret) else None,
           "diverge": diverge}
    if mae_static:
        out["mae_static"] = mae_static
        out["pred_range_low"] = float(last_nav * (1 + b_static - mae_static / 100))
        out["pred_range_high"] = float(last_nav * (1 + b_static + mae_static / 100))
    return out

def analyze_fund(code, year_q1=2026, month_q1=3, start_date="2025-08-01"):
    """单只基金全流程分析"""
    # 1. 持仓（Q1 当期 + Q2 当期）——get_holdings 返回 (holdings, source)，F10 失败时用缓存兜底
    h_q1, src_q1 = dfet.get_holdings(code, year_q1, month_q1)
    h_q2, src_q2 = dfet.get_holdings(code)
    if not h_q2:
        print(f"[{code}] Q2 持仓获取失败（实时+缓存均不可用）")
        return {"code": code, "error": "Q2 持仓获取失败"}
    q2_total = sum(x["pct"] for x in h_q2)
    us_q2 = sum(x["pct"] for x in h_q2 if x["market"] == "US")
    hk_q2 = sum(x["pct"] for x in h_q2 if x["market"] == "HK")
    src_note = f" 持仓来源: Q2={'缓存' if src_q2=='cache' else '实时'} Q1={'缓存' if src_q1=='cache' else '实时'}"
    print(f"[{code}] Q2十大 {q2_total:.1f}% (美股{us_q2:.1f}% 港{hk_q2:.1f}%){src_note}")

    # 2. 行情（Q1+Q2 并集）
    price_map = {}
    all_h = h_q1 + h_q2
    for h in all_h:
        if h["market"] == "SKIP":
            continue
        px = dfet.get_price_df(h["code"], h["market"])
        if px is not None:
            price_map[h["code"]] = px

    # 3. 净值 + 汇率 + 指数
    nav = dfet.get_nav(code, start_date)
    if nav is None or len(nav) == 0:
        print(f"[{code}] 净值获取失败")
        return {"code": code, "error": "净值获取失败"}
    nav = nav[nav["growth"].notna()].reset_index(drop=True)
    if len(nav) < 30:
        print(f"[{code}] 净值样本不足")
        return {"code": code, "error": "净值样本不足"}
    fx_df = dfet.get_usdcnh()
    ndx_df = dfet.get_index(".NDX")

    # 4. 静态披露权重预测（分区间）
    mask_q1 = (nav["date"] >= pd.Timestamp("2026-04-01")) & (nav["date"] <= pd.Timestamp("2026-06-30"))
    mask_q2 = nav["date"] >= pd.Timestamp("2026-07-01")
    pred_static = np.full(len(nav), np.nan)
    if mask_q1.sum() >= 10:
        pred_static[mask_q1] = basket_returns(nav[mask_q1], h_q1, price_map, fx_df)
    if mask_q2.sum() >= 10:
        pred_static[mask_q2] = basket_returns(nav[mask_q2], h_q2, price_map, fx_df)
    stat_static = evaluate(nav, pred_static)

    # 5. 滚动 NNLS（Q2 区间 7/1 后）
    pred_roll, last_w, full = rolling_nnls(nav, h_q2, price_map, fx_df, window=60)
    stat_roll = None
    if pred_roll is not None:
        # 对齐到 Q2 区间
        nav_roll = nav[nav["date"].isin(pred_roll.index)].copy()
        pred_aligned = pred_roll.reindex(nav_roll["date"]).values
        stat_roll = evaluate(nav_roll, pred_aligned)

    # 6. 指数回归（近6月 NDX β）
    beta6 = index_beta(nav, ndx_df, start_date="2026-02-10")

    # 7. 前瞻预测：用美股最近收盘（统一 us_last）预测对应净值日涨跌
    mae_static = stat_static["mae"] if stat_static else None
    us_last = dfet.us_last_trade_date()
    pred_next = predict_next(nav, h_q2, price_map, fx_df,
                             nnls_weight=last_w, mae_static=mae_static, us_last=us_last,
                             ndx_df=ndx_df)
    if pred_next is not None:
        print(f"[{code}] 预测 {pred_next['next_date'].date()}(US基准{us_last}): "
              f"静态{pred_next['pred_static']*100:+.2f}% 滚动{pred_next['pred_nnls']*100 if pred_next['pred_nnls'] is not None else float('nan'):+.2f}%")
    else:
        print(f"[{code}] 该期净值已公布（US基准 {us_last}），无新预测")

    return {"code": code, "q2_total": round(q2_total, 1), "us_pct": round(us_q2, 1),
            "hk_pct": round(hk_q2, 1), "price_n": len(price_map),
            "holdings": h_q2, "holdings_source": {"q2": src_q2, "q1": src_q1},
            "static": stat_static, "roll": stat_roll,
            "nnls_weight": {k: round(v, 4) for k, v in last_w.items()} if last_w else None,
            "ndx_beta": beta6, "skipped": [h["code"] for h in all_h if h["market"] == "SKIP"],
            "us_last": str(us_last), "predict": pred_next}
