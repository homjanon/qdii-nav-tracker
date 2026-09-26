#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QDII 基金净值跟踪 - 核心分析模块

功能：
1. 静态披露权重预测：用当期二十大持仓 × 美股日收益（lag=0 对齐）+ USDCNH 折算
2. 滚动 NNLS 动态权重：walk-forward 重估二十大权重，追踪调仓，提升幅度预测精度
3. 披露真实性验证：披露权重回测 R² / 方向一致率（与历史对比）
4. 美股含量评估：披露美股占比 + 指数回归 NDX β
"""
import numpy as np
import pandas as pd
from scipy.optimize import nnls
import data_fetcher as dfet

US_CODES_POOL = None  # 动态识别，不硬编码

def _weights(holdings):
    """披露权重 {code: pct/100}，仅保留有行情源的美股/港股/A股/日韩股
    2026-08-26：加入 JP/KR——净值日定价含 T 日日韩收盘（与美股/港/A股同基准），此前日韩被排除是预测盲区"""
    w = {}
    for h in holdings:
        if h["market"] in ("US", "HK", "CN", "JP", "KR"):
            w[h["code"]] = h["pct"] / 100
    return w

def basket_returns(nav, holdings, price_map, fx_df, lag=0):
    """披露权重篮子收益（对齐净值日期）

    2026-09-05 容错改造：单成分当日 NaN / 无行情 → 跳过该成分并以实际参与权重归一化，
    不再"一刀切"丢弃整行（二十大口径下成分多，日韩等源偶发缺口不应废掉整日预测）

    2026-09-26 两处改造（与原逐日循环**完全等价**，实测最大差 0.000000）：
      ① **向量化**：原实现逐日 × 逐标的调用 asof_ret（300 日 × 20 标的 = 6000 次，每次
         都要 copy / pct_change / 排序 / 去重整段序列）→ 实测 25.1s；改为每标的 1 次 → 0.42s（59×）。
      ② **休市日修正**：asof_ret 语义是「取 ≤ D 的最近交易日收益」，对休市日会返回**前一日**
         收益（旧值冒充——实测 600183 在 2026-09-25 A股休市日返回 −4.34%，实为 9/24 的值）。
         现在按市场日历把休市日收益置 **0** 并**保留其权重**；若当缺失剔除，会把该市场敞口
         摊到其他市场 → **放大**预测值。数据缺失（源失败/滞后）仍走 NaN + 归一化，两者性质不同。
    """
    w = _weights(holdings)
    total = np.full(len(nav), np.nan)
    if not w:
        return total
    wsum = sum(w.values())
    dates = pd.DatetimeIndex(nav["date"])
    mk = {h["code"]: h.get("market") for h in holdings}

    cols, wts = [], []
    for c, wgt in w.items():
        px = price_map.get(c)
        if px is None:
            continue
        r = np.asarray(dfet.asof_ret(px, dates, lag), dtype=float)
        market = mk.get(c)
        mask = dfet.market_open_mask(market, dates) if market else None
        if mask is not None:
            mask = np.asarray(mask, dtype=bool)
            # ① 休市 → 收益 0（价格确定没变；保留权重，不放大）
            r = np.where(mask, r, 0.0)
            # ② 开市但序列未覆盖该日（数据滞后）→ NaN（视为缺失，交归一化处理）
            try:
                last_px = pd.Timestamp(pd.to_datetime(px["date"]).max())
                r = np.where(mask & (dates > last_px), np.nan, r)
            except Exception:
                pass
        cols.append(r)
        wts.append(float(wgt))
    if not cols:
        return total

    M = np.column_stack(cols)
    W = np.asarray(wts, dtype=float)
    valid = ~np.isnan(M)
    num = np.nansum(np.where(valid, M * W, 0.0), axis=1)
    den = np.sum(np.where(valid, W, 0.0), axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        factor = np.where(den > 0, wsum / np.where(den > 0, den, 1.0), np.nan)
    out = num * factor
    out = np.where(den > 0, out, np.nan)
    # 汇率（按中国日历：非 A股交易日 → 中行牌价无当日价 → 当日汇率变动按 0）
    if fx_df is not None:
        fxr = np.asarray(dfet.asof_ret(fx_df, dates, lag), dtype=float)
        cn = dfet.market_open_mask("CN", dates)
        if cn is not None:
            fxr = np.where(np.asarray(cn, dtype=bool), fxr, 0.0)
        out = out + wsum * np.where(np.isnan(fxr), 0.0, fxr)
    return out

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
    返回: preds Series + 最新权重 dict

    2026-09-25：**汇率（FX）不再作为独立特征列进入 NNLS**。三条实测证据：
      ① A/B 回测（3 只基金）去掉后 MAE 不变或微降：0.816→0.816 / 0.991→0.930 / 0.651→0.648；
      ② 拟合权重极不稳定且出现经济不可能值（024239 中位 6.86、97.9% 期数 >1；
         汇率敞口物理上限 = 100% 持仓，即权重 ≤ 1）；
      ③ 中行牌价日收益与基金净值的相关 ≈ 0（5 只基金 0.001~0.084）→ 日频无信息量。
    汇率仍有贡献的地方是**静态篮子**（`basket_returns` / `predict_next` 的 `wsum × fxr`）：
    那里敞口 = 持仓比例，经济含义明确，故**保留**。
    （`fx_df` 参数保留以兼容调用方签名，此函数内不再使用。）
    """
    w0 = _weights(holdings)
    codes = [c for c in w0 if c in price_map]
    if not codes:
        return None, None, None
    # 构建收益矩阵（仅持仓成分）
    X = pd.DataFrame({"date": nav["date"]})
    for c in codes:
        X[c] = dfet.asof_ret(price_map[c], nav["date"])
    X = X.set_index("date")
    # 2026-09-05 健壮性：剔除整列全 NaN 的成分（数据源缺口，如日韩限流），
    # 否则 dropna() 会连带删掉全部有效行，NNLS 直接无样本
    X = X.dropna(axis=1, how="all")
    codes = list(X.columns)
    if not codes:
        return None, None, None  # 全部成分无数据 → NNLS 无特征可解
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
        try:
            w_nn, _ = nnls(Xh, yh)
        except Exception:
            continue
        x_today = X.loc[dates[i]].values
        if np.any(np.isnan(x_today)):
            continue
        preds[dates[i]] = float(w_nn @ x_today)
        last_w = dict(zip(codes, w_nn))
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
                 us_last=None, ndx_df=None, provisional=False):
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

    # 缺口门控（2026-09-24）：依赖标的在最近 4 天内存在「未修复缺口」→ 当日预测输入不可信，跳过
    # 场景：Yahoo 返回整行空值K线，且 东财/新浪/腾讯 全部取不到该日 → 上游以 NaN 占位，
    #       此时任何预测都建立在错误/缺失的因子收益上（宁可不出预测，也不出错的）
    _deps = {c for c, wt in w.items() if wt and wt > 0}
    _deps.update({c for c in (nnls_weight or {}) if c != "FX"})
    _deps.add("指数 .NDX")
    _gaps = dfet.unresolved_gaps(_deps, since=next_d - pd.Timedelta(days=4))
    if _gaps:
        _detail = ", ".join(f"{g['symbol']}@{g['date']}" for g in _gaps[:6])
        print(f"    ⛔ [缺口门控] 依赖标的近 4 天存在未修复缺口 → 跳过当日预测（{_detail}）")
        return {"blocked": True, "reason": f"数据缺口未修复: {_detail}",
                "next_date": next_d, "us_last": us_last}
    mk = {h["code"]: h.get("market") for h in holdings}
    b_static = 0.0
    contributors = []
    holiday, stale = [], []   # 休市成分（按 0 计）/ 数据滞后成分（剔除）
    for code, wgt in w.items():
        px = price_map.get(code)
        if px is None:
            continue
        market = mk.get(code)
        opened = dfet.is_market_open(market, next_d) if market else None
        if opened is False:
            # 休市（2026-09-26）：asof_ret 会返回**前一日**收益（旧值冒充，实测 600183 在
            # 2026-09-25 返回 −4.34% 实为 9/24 的值）→ 这里按 0 计（价格确定没变），
            # 且**保留权重**（不能当缺失剔除，否则会把该市场敞口摊给其他市场 → 放大预测）
            holiday.append(code)
            contributors.append({"code": code, "weight": wgt, "ret": 0.0, "contrib": 0.0})
            continue
        # 该市场开市、但行情序列尚未覆盖目标日 → 数据未到（源滞后）→ 剔除（与"缺失"同处理）
        try:
            if opened is True and pd.Timestamp(pd.to_datetime(px["date"]).max()) < next_d:
                stale.append(code)
                continue
        except Exception:
            pass
        r = dfet.asof_ret(px, [next_d])[0]
        if np.isnan(r):
            continue
        b_static += wgt * r
        contributors.append({"code": code, "weight": wgt, "ret": r, "contrib": wgt * r})
    fxr = np.nan
    if fx_df is not None:
        fxr = dfet.asof_ret(fx_df, [next_d])[0]
        if dfet.is_market_open("CN", next_d) is False:
            fxr = 0.0   # 中国休市 → 中行牌价无当日价 → 当日汇率变动按 0（同防旧值冒充）
        if not np.isnan(fxr):
            b_static += wsum * fxr
    if holiday:
        print(f"    [休市按0] {len(holiday)} 个成分: {', '.join(holiday[:8])}")
    if stale:
        print(f"    ⚠ [数据未到] {len(stale)} 个成分已剔除: {', '.join(stale[:8])}")
    contributors.sort(key=lambda x: x["contrib"], reverse=True)

    # 方向背离检测：NDX 当日收益与预测方向相反 → 提示谨慎
    ndx_ret = np.nan
    diverge = False
    if ndx_df is not None:
        ndx_ret = dfet.asof_ret(ndx_df, [next_d])[0]
        if not np.isnan(ndx_ret) and not np.isnan(b_static):
            diverge = np.sign(b_static) != np.sign(ndx_ret)

    # 滚动 NNLS 权重预测（2026-09-25：权重表已不含 FX，见 rolling_nnls 说明）
    b_nnls = None
    if nnls_weight:
        b_nnls = 0.0
        for code, wgt in nnls_weight.items():
            px = price_map.get(code)
            if px is None:
                continue
            # 休市修正（2026-09-26 补，与静态分支同口径）：休市 → 该成分收益按 0（跳过累加）
            # 否则 asof_ret 会拿前一交易日收益冒充（旧值噪声），与 pred_static 口径不一致
            market = mk.get(code)
            if market and dfet.is_market_open(market, next_d) is False:
                continue
            r = dfet.asof_ret(px, [next_d])[0]
            if np.isnan(r):
                continue
            b_nnls += wgt * r

    out = {"next_date": next_d, "last_date": last_date, "last_nav": last_nav,
           "provisional": bool(provisional),
           "holiday_symbols": holiday, "stale_symbols": stale,
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

def analyze_fund(code, year_q1=2026, month_q1=3, start_date="2025-08-01",
                 holdings_proxy=None, provisional=False):
    """单只基金全流程分析

    holdings_proxy（2026-09-18 加入）：持仓代理代码。ETF 联接基金自身的 F10
      「股票投资明细」可能长期停更（真实持仓是持有的目标 ETF 份额），此时用
      其跟踪的目标 ETF（如 017093 → 159509）的当期持仓作为代理做分析；
      **净值仍用基金自身**。代理标的为完全复制型 ETF，持仓即目标指数成分。

    provisional（2026-09-26 加入）：目标净值日（= us_last）非 A股交易日时为 True。
      此时该日基金不公布净值 → 预测**不对应任何真实净值**，仅作「参考值」展示：
      由 run_daily 保证不落库、不进验证统计；分析侧行为与正常预测一致（只是打标），
      但休市市场按 0 计、汇率按中国日历处理（详见 basket_returns / predict_next）。
    """
    hold_code = holdings_proxy or code
    # 1. 持仓（当期 + 上一期）——get_holdings 返回 (holdings, source, period)
    #    source: live 实时 / cache-fresh 缓存复用（未到重新校验期，0 请求）/ cache 失败兜底
    h_q1, src_q1, _p1 = dfet.get_holdings(hold_code, year_q1, month_q1)
    h_q2, src_q2, period = dfet.get_holdings(hold_code)
    if not h_q2:
        print(f"[{code}] Q2 持仓获取失败（实时+缓存均不可用）")
        return {"code": code, "error": "Q2 持仓获取失败", "holdings_proxy": holdings_proxy}
    # 期次校验（2026-09-18）：持仓数据过期（如联接基金自身 F10 停更）→ 跳过预测并告警
    # 报告期由 get_holdings 一并返回（2026-09-25 合并），不再单独发一次 F10 请求
    if dfet.is_period_stale(period):
        pa = f"{period[0]}Q{period[1]}" if period else "未知"
        print(f"[{code}] ⚠️ 持仓数据过期（{pa}，来源 {hold_code}），跳过预测（避免静默错误）")
        return {"code": code, "error": f"持仓数据过期（{pa}）", "holdings_proxy": holdings_proxy}
    if period:
        print(f"[{code}] 持仓期次: {period[0]}Q{period[1]}" + (f"（代理自 {hold_code}）" if holdings_proxy else ""))
    q2_total = sum(x["pct"] for x in h_q2)
    us_q2 = sum(x["pct"] for x in h_q2 if x["market"] == "US")
    hk_q2 = sum(x["pct"] for x in h_q2 if x["market"] == "HK")
    _srctxt = {"live": "实时", "cache-fresh": "缓存复用", "cache": "缓存兜底", "none": "无"}
    src_note = " 持仓来源: 当期=%s 上期=%s" % (_srctxt.get(src_q2, src_q2), _srctxt.get(src_q1, src_q1))
    print(f"[{code}] Q2二十大 {q2_total:.1f}% (美股{us_q2:.1f}% 港{hk_q2:.1f}%){src_note}")

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
                             ndx_df=ndx_df, provisional=provisional)
    blocked_reason = None
    if pred_next is not None and pred_next.get("blocked"):
        # 缺口门控命中（2026-09-24）：当日不出预测，原因写入报告供页面/日志暴露
        blocked_reason = pred_next.get("reason")
        print(f"[{code}] ⛔ 跳过当日预测：{blocked_reason}")
        pred_next = None
    elif pred_next is not None:
        print(f"[{code}] 预测 {pred_next['next_date'].date()}(US基准{us_last}): "
              f"静态{pred_next['pred_static']*100:+.2f}% 滚动{pred_next['pred_nnls']*100 if pred_next['pred_nnls'] is not None else float('nan'):+.2f}%")
    else:
        print(f"[{code}] 该期净值已公布（US基准 {us_last}），无新预测")

    return {"code": code, "q2_total": round(q2_total, 1), "us_pct": round(us_q2, 1),
            "hk_pct": round(hk_q2, 1), "price_n": len(price_map),
            "holdings": h_q2, "holdings_source": {"q2": src_q2, "q1": src_q1},
            "holdings_proxy": holdings_proxy,
            "holdings_period": f"{period[0]}Q{period[1]}" if period else None,
            "static": stat_static, "roll": stat_roll,
            "nnls_weight": {k: round(v, 4) for k, v in last_w.items()} if last_w else None,
            "ndx_beta": beta6, "skipped": [h["code"] for h in all_h if h["market"] == "SKIP"],
            "us_last": str(us_last), "predict": pred_next, "predict_blocked": blocked_reason}
