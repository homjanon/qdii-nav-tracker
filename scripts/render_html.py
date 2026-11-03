#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_html.py · 将 QDII 净值跟踪结果渲染为自包含静态 HTML 页面

输入：
  --json      output/daily_report.json（最新结果）
  --history   output/predictions.jsonl（累积预测历史，含已回填的 actual）
  --out       生成的 HTML 路径（如 docs/index.html）

输出：自包含 index.html（内联 CSS + Chart.js CDN），可直接由 GitHub Pages 发布。
区块：顶部标题 → 今晚净值预测（核心）→ 历史预测验证 → 美股含量总览（图表）→ 持仓质量表 → 疑似调仓
"""
import argparse, json, os, html
import datetime

CST = datetime.timezone(datetime.timedelta(hours=8))

def _load_fund_names():
    """基金名称：优先读 config/funds.json（与 run_daily 配置化一致），缺失回退内置"""
    DEFAULT = {"002891": "华夏移动互联", "008254": "华宝致远C", "014002": "浦银全球智能C",
               "015202": "汇添富全球移动C", "016702": "银华海外数字C", "018147": "建信新兴C",
               "021277": "广发全球精选C", "021842": "国富全球科技C", "022184": "富国全球互联网C",
               "080006": "长盛环球行业A", "024239": "华夏全球科技C", "017093": "景顺纳科C"}
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "funds.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        items = cfg.get("funds") or []
        names = {str(x["code"]): str(x.get("name", "")) for x in items if x.get("code")}
        if names:
            return names
    except Exception:
        pass
    return DEFAULT

FUND_NAMES = _load_fund_names()

CSS = """
:root{--bg:#f3f5f9;--card:#fff;--ink:#1f2937;--sub:#64748b;--line:#e5eaf1;
--accent:#2563eb;--up:#dc2626;--down:#16a34a;--warn:#d97706;
--shadow:0 1px 3px rgba(15,23,42,.06),0 8px 24px rgba(15,23,42,.06);}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);line-height:1.6;padding:28px 16px;-webkit-font-smoothing:antialiased;}
.wrap{max-width:960px;margin:0 auto;}
header{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px;margin-bottom:20px;}
h1{font-size:22px;font-weight:800;letter-spacing:-.3px;}
.sub{color:var(--sub);font-size:13px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;
box-shadow:var(--shadow);margin-bottom:18px;}
.card h2{font-size:15px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:8px;}
.ndx-badge{font-size:11px;font-weight:600;margin-left:auto;padding:3px 10px;border-radius:99px;background:#f8fafc;border:1px solid var(--line);color:var(--sub);display:flex;align-items:center;gap:5px;}
.ndx-badge b{color:var(--ink);font-size:12px;}
.ndx-badge.dq-warn{background:#fff7ed;border-color:#fed7aa;color:#c2410c;}
.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;background:#eef2ff;color:var(--accent);}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;}
.fund-card{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:#fafbfe;}
.fund-card .fname{font-size:13px;font-weight:600;color:var(--ink);}
.fund-card .fcode{font-size:11px;color:var(--sub);margin-top:1px;}
.fund-card .pred{font-size:26px;font-weight:800;margin:8px 0 2px;letter-spacing:-.5px;}
.fund-card .meta{font-size:12px;color:var(--sub);display:flex;justify-content:space-between;margin-top:6px;padding-top:8px;border-top:1px dashed var(--line);}
.fund-card .purchase{font-size:11px;color:var(--sub);display:flex;justify-content:space-between;margin-top:8px;padding-top:8px;border-top:1px dashed var(--line);}
.fund-card .purchase .amt{font-weight:700;color:var(--blue);}
.fund-card .purchase .amt.paused{color:var(--sub);text-decoration:line-through;}
.up{color:var(--up);} .down{color:var(--down);}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:14px;}
.stat{background:#f8fafc;border:1px solid var(--line);border-radius:10px;padding:12px 16px;text-align:center;}
.stat .num{font-size:24px;font-weight:800;}
.stat .lbl{font-size:12px;color:var(--sub);margin-top:2px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;}
th:first-child,td:first-child{text-align:left;}
th{color:var(--sub);font-weight:600;font-size:12px;background:#f8fafc;}
tr:hover td{background:#f8fafc;}
.ok{color:var(--down);font-weight:700;} .no{color:var(--up);font-weight:700;}
.legend{display:flex;gap:16px;font-size:12px;color:var(--sub);margin-bottom:8px;}
.legend span{display:flex;align-items:center;gap:5px;}
.legend i{width:10px;height:10px;border-radius:2px;display:inline-block;}
details{margin-top:10px;border:1px solid var(--line);border-radius:10px;padding:10px 14px;background:#fafbfe;}
summary{cursor:pointer;font-size:13px;font-weight:600;color:var(--ink);}
.chart-box{position:relative;width:100%;height:320px;}
@media(max-width:640px){.fund-card .pred{font-size:22px;}}
"""

def fmt_pct(v, digits=2):
    """百分比格式化：0.0107 → '+1.07%'"""
    if v is None:
        return "-"
    return f"{v*100:+.{digits}f}%"

def fmt_pp(v, digits=2):
    """百分点格式化：err 是小数 0.0110 → '+1.10pp'（×100）
    ⚠️ 曾漏乘 100 导致网页误差列显示 0.01pp（差100倍），2026-08-20 修复"""
    if v is None:
        return "-"
    return f"{v*100:+.{digits}f}pp"

def pred_color(v):
    if v is None:
        return ""
    return "up" if v > 0 else ("down" if v < 0 else "")

def _as_bool(v):
    """安全布尔转换：兼容 JSON 里的原生 bool 和字符串 'True'/'False'
    ⚠️ 曾因 default=str 把 np.bool_ 序列化成 'False'，bool('False')==True 误判背离"""
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)

def esc(s):
    return html.escape(str(s))

def load_history(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows

def render(report, history, out_path, full_holdings=None):
    date = report.get("date", "")
    gen = report.get("generated_at", "")
    funds = report.get("funds", {})
    verify = report.get("verify", {})
    purchase = report.get("purchase") or {}  # {code: {"status","limit"}} 申购限额（2026-08-18 加入）
    # 口径一致性（2026-11-03）：页面只展示「当前基金清单」的记录。
    # 已从 config/funds.json 移出的基金（如 012922）历史记录仍留在 predictions.jsonl，
    # 但不再出现在验证表/待验证计数里——否则会永久占着「每只基金最近 2 条」的名额，
    # 且名称查不到而显示裸代码。统计侧由 run_daily.verify_history 同步过滤。
    _valid_codes = set(FUND_NAMES)
    history = [h for h in history if h.get("code") in _valid_codes]
    hist_verified = [h for h in history if h.get("actual") is not None]

    # ---- 全持仓档案口径（年报/半年报）汇总，供"美股含量"结构外推使用（2026-11-03）----
    # {code: {"total": 累计占净值%, "us": 美股占净值%, "period": 报告期,
    #         "by_code": {标的: 占净值%}, "us_codes": {美股标的}}}
    full_map = {}
    if full_holdings:
        for _c, _e in (full_holdings.get("funds") or {}).items():
            _hs = _e.get("holdings") or []
            if not _hs:
                continue
            full_map[_c] = {
                "total": sum((x.get("pct") or 0) for x in _hs),
                "us": sum((x.get("pct") or 0) for x in _hs if x.get("market") == "US"),
                "by_code": {x.get("code"): (x.get("pct") or 0) for x in _hs},
                "us_codes": {x.get("code") for x in _hs if x.get("market") == "US"},
                "period": (_e.get("report_date")
                           or full_holdings.get("report_date") or ""),
            }
    # 参考值模式（2026-09-26）：目标净值日非 A股交易日 → 该日无净值，预测仅供观看、不参与验证
    provisional = bool(report.get("provisional"))
    prov_banner = ("" if not provisional else
                   '<div class="card" style="border-left:4px solid #d97706;background:#fffbeb">'
                   '<div style="font-weight:700;color:#92400e">⚠️ 参考值模式（本日无对应净值）</div>'
                   '<div class="sub" style="margin-top:4px">目标净值日非 A股交易日 → QDII 基金当日不公布净值，'
                   '下列预测<b>不对应任何真实净值</b>，仅供观看参考、<b>不落库、不参与验证统计</b>。'
                   '休市市场当日收益按 0 计（非缺失剔除），汇率按中国日历处理。</div></div>')

    # ---- 今晚预测卡片（保留全部基金：待验证显示预测，已公布显示预测vs实际）----
    pred_cards = []
    for code, r in funds.items():
        if "error" in r:
            continue
        p = r.get("predict")
        diverge = _as_bool(p.get("diverge")) if p else False
        src_cache = (r.get("holdings_source") or {}).get("q2") == "cache"
        if p is not None:
            # 有预测：待验证
            pn = p.get("pred_nnls")
            pred_cards.append({
                "code": code, "name": FUND_NAMES.get(code, code),
                "pred": p["pred_static"], "pred_nnls": pn,
                "pred_nav": p["pred_nav_static"], "last_nav": p["last_nav"],
                "next_date": str(p["next_date"])[:10],
                "status": "pending", "actual": None, "hit": None, "err": None,
                "diverge": diverge, "src_cache": src_cache,
                "purchase": purchase.get(code),
            })
        else:
            # 已公布：从 history 找该基金最新已验证记录（预测 vs 实际对照）
            recs = [h for h in history if h.get("code") == code and h.get("actual") is not None]
            if recs:
                rec = recs[-1]
                pred_cards.append({
                    "code": code, "name": FUND_NAMES.get(code, code),
                    "pred": rec["pred_static"], "pred_nnls": rec.get("pred_nnls"),
                    "pred_nav": rec.get("pred_nav_static"), "last_nav": rec.get("actual_nav"),
                    "next_date": rec["pred_date"],
                    "status": "verified", "actual": rec["actual"], "hit": rec.get("hit"),
                    "err": rec.get("err"), "diverge": diverge, "src_cache": src_cache,
                    "purchase": purchase.get(code),
                })
    pred_cards.sort(key=lambda x: (0 if x["status"] == "pending" else 1, -x["pred"]))

    cards_html = []
    for c in pred_cards:
        cls = pred_color(c["pred"])
        arrow = "▲" if c["pred"] > 0 else ("▼" if c["pred"] < 0 else "—")
        nnls_s = fmt_pct(c["pred_nnls"]) if c["pred_nnls"] is not None else "-"
        diverge_note = '<span style="color:var(--warn);font-size:11px">⚠️与大盘背离</span>' if c.get("diverge") else ""
        cache_note = '<span style="color:var(--sub);font-size:11px">持仓缓存</span>' if c.get("src_cache") else ""
        # 申购限额（东财 fund_purchase_em）：仅金额，暂停申购显示「暂停」（2026-08-18 加入）
        _pur = c.get("purchase") or {}
        _lim = _pur.get("limit")
        if _pur.get("status") == "暂停申购":
            purchase_html = '<div class="purchase"><span>申购限额</span><span class="amt paused">暂停</span></div>'
        elif _lim is not None:
            _amt = f'{_lim:.0f}元' if _lim >= 1 else f'{_lim:g}元'
            if _lim >= 10000:
                _amt = f'{_lim/10000:.0f}万'
            purchase_html = f'<div class="purchase"><span>申购限额</span><span class="amt">{_amt}</span></div>'
        else:
            purchase_html = '<div class="purchase"><span>申购限额</span><span class="amt">不限购</span></div>'
        if c["status"] == "pending":
            status_badge = f'<span class="badge">待公布</span> {diverge_note} {cache_note}'
            actual_html = f'<div class="meta"><span>最新净值 {c["last_nav"]:.4f}</span><span>{esc(c["next_date"])}公布</span></div>'
        else:
            # 综合命中：方向对 且 |误差|≤1.0pp 才算 ✓
            hit = bool(c.get("hit")) and abs(c.get("err") or 0) <= 0.010
            mark = '<span class="ok">✓</span>' if hit else '<span class="no">✗</span>'
            status_badge = f'<span class="badge" style="background:#f0fdf4;color:#166534">已公布 {mark}</span> {cache_note}'
            actual_html = (f'<div class="meta"><span>实际 {fmt_pct(c["actual"])}</span>'
                           f'<span>误差 {fmt_pp(c["err"])}</span></div>'
                           f'<div class="meta"><span>实际净值 {c["last_nav"]:.4f}</span><span>{esc(c["next_date"])}公布</span></div>')
        cards_html.append(f"""
        <div class="fund-card">
          <div class="fname">{esc(c['name'])} <span class="fcode">{c['code']}</span> {status_badge}</div>
          <div class="pred {cls}">{arrow} {fmt_pct(c['pred'])}</div>
          <div class="meta"><span>滚动NNLS {nnls_s}</span><span>预测净值<br/><b>{c['pred_nav']:.4f}</b></span></div>
          {actual_html}
          {purchase_html}
        </div>""")
    pred_block = "\n".join(cards_html) if cards_html else "<p class='sub'>暂无预测数据</p>"

    # NDX 当日收盘（右上角对照：点位 + 涨跌幅；2026-08-26 加入）
    # 2026-09-24：涨跌幅来自行情接口自带值（见 data_fetcher.index_quote），
    #   不再由日线序列相减（序列缺口会把多日累计当单日）
    ndx_html = ""
    ndx = report.get("ndx")
    if ndx and ndx.get("close"):
        _pct = ndx.get("pct")
        _cls = "up" if (_pct or 0) > 0 else ("down" if (_pct or 0) < 0 else "")
        _arrow = "▲" if (_pct or 0) > 0 else ("▼" if (_pct or 0) < 0 else "—")
        _pct_s = f"{_pct:+.2f}%" if _pct is not None else "-"
        _src = ndx.get("source") or ""
        _tip = "纳指100 当日收盘，用于对照背离提示"
        if _src:
            _tip += f"（数据源：{_src}）"
        if _pct is None:
            _tip += "；涨跌幅暂不可得——行情源异常时宁可不显示，也不拿有缺口的序列相减"
        ndx_html = (f'<span class="ndx-badge" title="{esc(_tip)}">'
                    f'NDX {esc(ndx.get("date",""))} <b>{ndx["close"]:,.0f}</b> '
                    f'<span class="{_cls}">{_arrow} {_pct_s}</span></span>')

    # 数据质量告警（2026-09-24）：存在「未修复缺口」→ 页面可见提示（防静默出错）
    _dq = report.get("data_quality") or {}
    _dqc = _dq.get("counts") or {}
    _n_unres = _dqc.get("unresolved") or 0
    if _n_unres:
        _u_txt = "、".join((g.get("symbol", "") + "@" + g.get("date", ""))
                          for g in (_dq.get("unresolved") or [])[:5])
        _u_tip = "存在未修复的数据缺口：" + _u_txt + "；相关基金当日已跳过预测"
        ndx_html += (f'<span class="ndx-badge dq-warn" title="{esc(_u_tip)}">'
                     f'⚠️ 数据缺口 {_n_unres}</span>')

    # ---- 历史验证 ----
    v_n = verify.get("n", 0)
    v_dir = verify.get("dir_acc")
    v_mae = verify.get("mae")
    v_mag = verify.get("mag_acc")       # 幅度命中率 ±1pp
    v_combo = verify.get("combo_acc")   # 综合精度命中
    v_cover = verify.get("cover")       # 区间覆盖率
    v_band = verify.get("band")         # 误差带宽度 pp
    v_bias = verify.get("bias")         # 平均误差 pp
    # 待验证：预测净值日已过但尚未回填（今晚将公布）
    pending_verify = [h for h in history if h.get("actual") is None]
    pv_count = len(pending_verify)
    # 已验证记录全部（按验证顺序：pred_date 升序），默认显示最近 5 条，可展开全部
    hist_by_key = {(h.get("code"), h.get("pred_date")): h for h in history}
    verified_all = [h for h in history if h.get("actual") is not None]
    verified_all.sort(key=lambda x: (x.get("pred_date", ""), x.get("code", "")))
    verified_recent = verified_all[-5:][::-1]  # 最近 5 条，最新在前

    def _vrow(v):
        # 综合命中：方向对 且 |误差|≤1.0pp 才算 ✓（2026-08-20 用户要求统一）
        hit = bool(v.get("hit")) and abs(v.get("err") or 0) <= 0.010
        mark = '<span class="ok">✓</span>' if hit else '<span class="no">✗</span>'
        hk = hist_by_key.get((v["code"], v["pred_date"]), {})
        run_date = str(hk.get("run_date", ""))[:10]
        return (f"<tr><td>{FUND_NAMES.get(v['code'], v['code'])}</td><td>{esc(run_date)}</td>"
                f"<td>{esc(v['pred_date'])}</td>"
                f"<td>{fmt_pct(v['pred_static'])}</td><td>{fmt_pct(v.get('actual'))}</td>"
                f"<td>{mark}</td><td>{fmt_pp(v.get('err'))}</td></tr>")

    v_rows_show = "".join(_vrow(v) for v in verified_recent)
    # 查看更多：每只基金取各自最近 2 条已验证（12基金×2=24条，跨多日展示）
    # 2026-09-05 修复：原实现"全局最新 2N 条"只覆盖最近 2 天，早前验证被挤出表格
    per_fund_last2 = []
    _seen_code = {}
    for v in reversed(verified_all):  # 最新在前
        c = v["code"]
        if _seen_code.get(c, 0) < 2:
            per_fund_last2.append(v)
            _seen_code[c] = _seen_code.get(c, 0) + 1
    v_rows_all = "".join(_vrow(v) for v in per_fund_last2)
    v_table_head = ("<table><thead><tr><th>基金</th><th>预测来源日</th><th>净值日</th><th>预测</th>"
                    "<th>实际</th><th>命中</th><th>误差</th></tr></thead>")
    if v_rows_show:
        if len(verified_all) > 5:
            v_table = (v_table_head
                       + f"<tbody id='v-rows-show'>{v_rows_show}</tbody>"
                       + f"<tbody id='v-rows-all' style='display:none'>{v_rows_all}</tbody></table>"
                       + "<div style='margin-top:8px;text-align:center'>"
                       + "<button onclick=\"var a=document.getElementById('v-rows-all'),s=document.getElementById('v-rows-show'),b=this;"
                       + "if(a.style.display==='none'){a.style.display='';s.style.display='none';b.textContent='收起';}"
                       + "else{a.style.display='none';s.style.display='';b.textContent='查看更多';}\" "
                       + "style='cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--accent);"
                       + "border-radius:8px;padding:4px 16px;font-size:12px'>查看更多</button></div>")
        else:
            v_table = v_table_head + f"<tbody>{v_rows_show}</tbody></table>"
    else:
        v_table = "<p class='sub'>暂无已公布验证记录，明日起自动累积</p>"

    # ---- 美股含量图表数据（2026-11-03 A+B 改造）----
    # 背景：原先只画 us_pct = 当期披露持仓里美股"占净值比"直接相加。
    #   季报（Q1/Q3）只披露前十大 → 分母天然偏小，系统性低估。
    #   实测 080006 长盛环球行业A：季报十大口径 42.6%，半年报全持仓 85.8%（差 43pp）。
    # A 轨道：us_pct（当期披露口径，作为"下限"）
    # B 轨道：结构外推估算
    #   share = 全持仓档案中「当期十大没覆盖到的那部分」的美股占比
    #           = (全持仓美股 − 重叠标的在全持仓里的美股%) / (全持仓合计 − 重叠标的在全持仓里的合计%)
    #   gap   = 当期尚未被披露覆盖的仓位 = 全持仓合计 − 当期披露合计
    #   估算  = 当期披露美股% + gap × share
    #   为什么不是"直接加剩余美股绝对额"（v1/v2 踩过的两个坑）：
    #     ① 把同一只股票算两次 —— 当期十大里的 NVDA 在全持仓里也有，必须先扣重叠；
    #     ② 混用两个时点的权重绝对值 —— 当期十大权重（Q3）比上次披露（Q2）大时，
    #        再叠加 Q2 的剩余额会爆表（华宝致远C 曾估到 95.7%，而它全持仓总量只有 81.3%）。
    #        改用"剩余仓位 × 剩余美股占比"后，估算值天然 ≤ 全持仓合计。
    #   经济含义：把尚未披露覆盖的仓位，按其上次披露时的美股占比承接过来。
    #   若基金当期大举调仓，外推会滞后（已知代价，故同时保留 A 轨道与 NDX β 交叉校验）。
    chart_labels, chart_us, chart_beta = [], [], []
    chart_est, chart_full, chart_period = [], [], []
    chart_topper, chart_kind, chart_cov = [], [], []
    for code in funds:  # 动态取基金（与"今晚净值预测"一致，2026-08-21 修复 8→10）
        r = funds.get(code)
        if not r or "error" in r:
            continue
        _us_top = float(r.get("us_pct") or 0)
        _tot_top = float(r.get("q2_total") or 0)
        _f = full_map.get(code) or {}
        _est = _us_top
        if _f.get("by_code"):
            _top = r.get("holdings") or []
            _bys, _us_codes = _f["by_code"], _f["us_codes"]
            _ov_all = sum(_bys.get(x.get("code"), 0) for x in _top)
            _ov_us = sum(_bys.get(x.get("code"), 0) for x in _top if x.get("code") in _us_codes)
            _rest_total = max(0.0, _f["total"] - _ov_all)
            _rest_us = max(0.0, _f["us"] - _ov_us)
            _share = (_rest_us / _rest_total) if _rest_total > 0 else 0.0
            _gap = max(0.0, _f["total"] - _tot_top)
            _est = min(100.0, _us_top + _gap * _share)
        # 当期（季报/半年报）期次与档位 —— 2026-11-03 补：原来图里只有"当期披露"却没有期次，
        # 用户误把 tooltip 里"全持仓口径（2026-06-30）"当成了当期披露的日期。
        _per = str(r.get("holdings_period") or "").strip()
        _q = int(_per[-1]) if len(_per) >= 2 and _per[-1] in "1234" else 0
        chart_labels.append(f"{FUND_NAMES.get(code, code)}")
        chart_us.append(round(_us_top, 1))
        chart_est.append(round(_est, 1))
        # 全持仓口径只进 tooltip（不单独画柱，避免三柱过密）
        chart_full.append(f"{_f['us']:.1f}" if _f else "—")
        chart_period.append(_f.get("period", "") or "—")
        chart_topper.append(_per or "—")
        chart_kind.append("季报前十大" if _q in (1, 3)
                          else ("年报/半年报前二十大" if _q else "披露持仓"))
        chart_cov.append(round(_tot_top, 1))
        chart_beta.append(round((r.get("ndx_beta") or {}).get("ndx_beta", 0), 2))

    # 图例用的当期报告期（各基金通常一致，取众数）
    _tp_cnt = {}
    for _v in chart_topper:
        if _v and _v != "—":
            _tp_cnt[_v] = _tp_cnt.get(_v, 0) + 1
    top_period = sorted(_tp_cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if _tp_cnt else ""
    top_kind = ("季报前十大" if top_period[-1:] in ("1", "3")
                else ("年报/半年报前二十大" if top_period else ""))
    top_desc = (top_period + " " + top_kind).strip()
    full_period_any = ((full_holdings or {}).get("report_date") or "").strip()

    # ---- 30 日涨跌幅走势对比（2026-08-21 新增；相对区间首日累计涨跌%）----
    # 颜色自动分配（2026-08-26 升级）：预定义 16 色调色板 + 按 code 排序稳定分配，
    # 超限用 code 哈希生成色相——用户增减基金零维护，颜色永不重复
    # ⚠️ 固定色（用户确认过）：华宝=黑、易方达=橙、长盛=深青、华夏科技=棕褐，先占用，其余按序补
    TREND_PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#7c3aed", "#0891b2",
                     "#db2777", "#d97706", "#65a30d", "#6366f1", "#e11d48",
                     "#a3a000", "#475569"]
    TREND_FIXED = {"008254": "#111827", "012922": "#f97316",
                   "080006": "#0d9488", "024239": "#92400e"}
    trend = report.get("trend") or {}
    _color_map = dict(TREND_FIXED)
    _used = set(_color_map.values())
    _palette_iter = iter([c for c in TREND_PALETTE if c not in _used])
    for _c in sorted(trend.keys()):
        if _c in _color_map:
            continue
        try:
            _color_map[_c] = next(_palette_iter)
        except StopIteration:  # 超限：code 哈希 → 色相环（黄金角散列）
            _h = (int(_c) * 137.508) % 360
            _color_map[_c] = f"hsl({_h:.0f},70%,45%)"
    # 日期对齐：取各基金日期的并集（升序），缺失日补 null（避免 tooltip 少显示基金）
    all_dates = sorted({dd for td in trend.values() for dd in (td.get("dates") or [])})
    trend_dates, trend_sets = [], []
    for code, td in trend.items():
        if not td or len(td.get("dates", [])) < 2 or not td.get("pct"):
            continue
        # 映射 date -> pct，按公共轴对齐
        pct_map = dict(zip(td["dates"], td["pct"]))
        trend_dates = all_dates
        trend_sets.append({
            "label": FUND_NAMES.get(code, code),
            "data": [pct_map.get(dd) for dd in all_dates],  # 缺失日 None（Chart.js 显示为空）
            "borderColor": _color_map[code],
            "backgroundColor": _color_map[code],
            "borderWidth": 2,
            "pointRadius": 1.5,
            "tension": 0.2,
            "spanGaps": True,  # 缺失日连线（不中断）
        })
    trend_dates_json = json.dumps(trend_dates, ensure_ascii=False)
    trend_sets_json = json.dumps(trend_sets, ensure_ascii=False)

    # ---- 持仓质量表 ----
    q_rows = []
    def sorter_key(code):
        r = funds.get(code) or {}
        return -(r.get("us_pct") or 0)
    for code in sorted(funds.keys(), key=sorter_key):
        r = funds[code]
        if "error" in r:
            q_rows.append(f"<tr><td>{esc(code)}</td><td>错误: {esc(r.get('error',''))[:40]}</td></tr>")
            continue
        s = r.get("static") or {}
        rr = r.get("roll") or {}
        beta = (r.get("ndx_beta") or {}).get("ndx_beta")
        # 披露覆盖率（2026-11-03 新增）：季报只有前十大，覆盖率可能低至 20% 出头，
        #   此时静态预测的"篮子缩放"会让预测值系统性偏小 → 用灰色提示读者降低信任度。
        #   放在"披露美股%"单元格第二行（不新增列，避免手机端 8 列表格挤爆）。
        _cov = float(r.get("q2_total") or 0)
        _cov_attr = ("" if _cov >= 40
                     else " title='披露持仓仅覆盖 %.1f%% 仓位，静态预测参考价值下降' style='color:#b45309'" % _cov)
        q_rows.append(f"<tr><td>{FUND_NAMES.get(code, code)}<br/><span class='sub'>{code}</span></td>"
                      f"<td>{r.get('us_pct', 0):.1f}%"
                      f"<br/><span class='sub'{_cov_attr}>覆盖 {_cov:.1f}%</span></td>"
                      f"<td>{beta:.2f}</td>"
                      f"<td>{s.get('dir_acc', 0):.1f}%</td>"
                      f"<td>{s.get('mae', 0):.2f}pp</td>"
                      f"<td>{rr.get('mae', 0):.2f}pp</td>"
                      f"<td>{s.get('r2', 0):.3f}</td></tr>")
    q_table = ("<table><thead><tr><th>基金</th>"
               "<th>披露美股%<br/><span style='font-weight:400'>（覆盖仓位）</span></th>"
               "<th>NDX β</th><th>方向准确率</th>"
               "<th>静态MAE</th><th>滚动MAE</th><th>持仓R²</th></tr></thead>"
               f"<tbody>{''.join(q_rows)}</tbody></table>")

    # ---- 疑似调仓折叠（展示当期披露持仓，无变化标0）----
    # 2026-11-03：标题不再写死"二十大"——F10 返回最新报告期，季报（Q1/Q3）只披露前十大，
    #   硬写"二十大"会出现"二十大持仓 10 项"的自相矛盾。改为「期次 + 档位 + 实际条数」。
    adj_details = []
    for code, r in funds.items():
        if "error" in r:
            continue
        w = r.get("nnls_weight") or {}
        holdings = r.get("holdings", [])
        # 档位文案：季报（Q1/Q3）只有前十大；半年报/年报（Q2/Q4）才有前二十大
        _per = str(r.get("holdings_period") or "").strip()
        _q = int(_per[-1]) if len(_per) >= 2 and _per[-1] in "1234" else 0
        _tier = "十大" if _q in (1, 3) else ("二十大" if _q in (2, 4) else "")
        hold_desc = (f"{_per} {_tier}持仓 {len(holdings)} 项" if _per
                     else f"披露持仓 {len(holdings)} 项")
        disc = {x["code"]: x["pct"] for x in holdings}
        names = {x["code"]: x["name"] for x in holdings}
        # 最新涨跌幅（来自 predict.contributors；2026-08-26 新增列）
        p = r.get("predict") or {}
        contrib_ret = {x.get("code"): x.get("ret") for x in (p.get("contributors") or [])}
        rows = []
        changed = 0
        for x in sorted(holdings, key=lambda v: v["pct"], reverse=True):
            c = x["code"]
            d = x["pct"]
            wt = w.get(c, 0) or 0
            diff = wt * 100 - d
            if abs(diff) > 2:
                changed += 1
            flag = '<span class="ok">▲加仓</span>' if diff > 2 else ('<span class="no">▼减仓</span>' if diff < -2 else "")
            nm = names.get(c, "")
            # 最新涨跌幅单元格（红涨绿跌；无数据显示 -）
            ret = contrib_ret.get(c)
            if ret is not None and ret == ret:  # 非 NaN
                _cls = "up" if ret > 0 else ("down" if ret < 0 else "")
                ret_cell = f'<span class="{_cls}">{ret*100:+.2f}%</span>'
            else:
                ret_cell = "-"
            rows.append(f"<tr><td>{esc(c)}</td><td>{esc(nm)}</td><td>{d:.1f}%</td><td>{wt*100:.1f}%</td>"
                        f"<td>{diff:+.1f}% {flag}</td><td>{ret_cell}</td></tr>")
        proxy = r.get("holdings_proxy")
        tag = f" · 持仓代理自 {esc(proxy)}（穿透）" if proxy else ""
        adj_details.append(f"""
        <details>
          <summary>{FUND_NAMES.get(code, code)}（{code}）— {hold_desc} · 疑似调仓 {changed} 项{tag}</summary>
          <table style="margin-top:8px"><thead><tr><th>代码</th><th>名称</th><th>披露%</th><th>NNLS估计%</th><th>差异</th><th>最新涨跌幅</th></tr></thead>
          <tbody>{''.join(rows)}</tbody></table>
          <div style="margin-top:6px;text-align:right"><a href="#full-holdings-{esc(code)}" style="color:var(--accent);font-size:12px;text-decoration:none">📄 查看全部持仓档案 →</a></div>
        </details>""")
    adj_block = "\n".join(adj_details) if adj_details else "<p class='sub'>暂无数据</p>"

    # ---- 全部持仓档案（年报/半年报口径，静态披露档案，来自 output/holdings_full.json）----
    # 2026-11-03：板块更名（季报不进这个档案）；期次由 run_daily 自动判定，逐基金可不同
    full_details = []
    full_report_date = ""
    if full_holdings:
        full_report_date = (full_holdings.get("report_date") or "").strip()
        for code, entry in (full_holdings.get("funds") or {}).items():
            hs = entry.get("holdings") or []
            ts = entry.get("ts", "")
            _p = (entry.get("report_date") or full_report_date or "").strip()
            total = sum((x.get("pct") or 0) for x in hs)
            full_rows = []
            for x in sorted(hs, key=lambda v: v.get("seq", 0)):
                full_rows.append(f"<tr><td>{x.get('seq','')}</td><td>{esc(x.get('code',''))}</td>"
                                 f"<td>{esc(x.get('name',''))}</td><td>{x.get('pct',0):.2f}%</td></tr>")
            proxy2 = entry.get("holdings_proxy")
            tag2 = f" · 持仓代理自 {esc(proxy2)}（穿透）" if proxy2 else ""
            full_details.append(f"""
        <details id="full-holdings-{esc(code)}">
          <summary>{FUND_NAMES.get(code, code)}（{code}）— 全部持仓 {len(hs)} 项 · 累计 {total:.1f}% · {esc(_p) or "期次未知"} · 抓取 {esc(ts)}{tag2}</summary>
          <table style="margin-top:8px"><thead><tr><th>序号</th><th>代码</th><th>名称</th><th>占净值%</th></tr></thead>
          <tbody>{''.join(full_rows)}</tbody></table>
        </details>""")
    full_block = "\n".join(full_details) if full_details else "<p class='sub'>暂无全持仓数据（待披露期抓取）</p>"

    # ---- 组装 ----
    now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QDII 净值跟踪 · {esc(date)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div><h1>QDII 净值跟踪</h1><div class="sub">披露持仓 × 美股行情 · 静态 + 滚动NNLS</div></div>
  <div class="sub">报告日 {esc(date)}<br>更新 {esc(gen)}</div>
</header>

{prov_banner}

<div class="card">
  <h2>今晚净值预测 <span class="badge">{esc(date)}</span> {ndx_html}</h2>
  <div class="legend">
    <span><i style="background:var(--up)"></i>预测上涨</span>
    <span><i style="background:var(--down)"></i>预测下跌</span>
    <span style="display:flex;align-items:center;gap:5px;"><span style="font-size:11px;padding:1px 6px;border-radius:99px;background:#f0fdf4;color:#166534">已公布</span>已公布净值的显示预测 vs 实际对照</span>
    <span class="sub">静态=披露权重 · 滚动=60日NNLS · 基于美股最新收盘（lag=0）</span>
  </div>
  <div class="grid">{pred_block}</div>
</div>

<div class="card">
  <h2>历史预测验证 <span class="badge">{v_n} 条已验证</span></h2>
  <div class="legend">
    <span class="sub">闭环：早上预测 → 当晚净值公布 → 次日早上自动验证回填</span>
    <span class="sub">✓=方向对且|误差|≤1.0pp（综合命中）· 区间=实际落在预测±{f"{v_band:.2f}" if v_band is not None else "-"}pp误差带内</span>
  </div>
  <div class="stat-grid">
    <div class="stat"><div class="num">{f"{v_dir:.1f}%" if v_dir is not None else "-"}</div><div class="lbl">方向命中率</div></div>
    <div class="stat"><div class="num">{f"{v_mag:.1f}%" if v_mag is not None else "-"}</div><div class="lbl">幅度命中率±1pp</div></div>
    <div class="stat"><div class="num">{f"{v_combo:.1f}%" if v_combo is not None else "-"}</div><div class="lbl">综合精度命中</div></div>
    <div class="stat"><div class="num">{f"{v_cover:.0f}%" if v_cover is not None else "-"}</div><div class="lbl">区间覆盖率</div></div>
    <div class="stat"><div class="num">{f"{v_mae:.2f}" if v_mae is not None else "-"}</div><div class="lbl">MAE（pp）</div></div>
    <div class="stat"><div class="num">{pv_count}</div><div class="lbl">待验证（今晚公布后回填）</div></div>
  </div>
  {v_table}
</div>

<div class="card">
  <h2>美股含量总览</h2>
  <div class="legend" style="flex-wrap:wrap">
    <span><i style="background:var(--accent)"></i>估算美股含量%（结构外推）</span>
    <span><i style="background:#f59e0b;border-radius:99px"></i>当期披露%{f"（{esc(top_desc)}）" if top_desc else ""}</span>
    <span class="sub">估算 = 当期披露美股% + 未覆盖仓位按全持仓结构外推；NDX β 见 tooltip</span>
  </div>
  <div class="chart-box"><canvas id="usChart" role="img" aria-label="美股含量对比条形图">美股含量对比</canvas></div>
  <div class="sub" style="margin-top:8px"><b>两个期次不同，别混看</b>：当期披露{f"取 {esc(top_desc)}" if top_desc else "取最新报告期"}（季报只披露前十大 → 这是<b>下限</b>）；全持仓结构取最近年报/半年报{f"（{esc(full_period_any)}）" if full_period_any else ""}——季报不披露全持仓，所以这个期次必然滞后，仅用于补齐未披露的那部分仓位。NDX β &gt; 1 表示对纳指放大暴露。</div>
</div>

<div class="card">
  <h2>基金涨跌幅对比（近30日）</h2>
  <div class="legend">
    <span class="sub">相对区间首日的累计涨跌幅 % · 曲线越高涨得越好 · 随每日运行滚动更新</span>
  </div>
  <div class="chart-box"><canvas id="trendChart" role="img" aria-label="基金涨跌幅对比折线图">基金涨跌幅对比</canvas></div>
</div>

<div class="card">
  <h2>持仓质量（历史回测）</h2>
  {q_table}
</div>

<div class="card">
  <h2>疑似调仓（滚动NNLS vs 披露）</h2>
  {adj_block}
</div>

<div class="card">
  <h2>全部持仓档案 <span class="badge">年报 / 半年报{f" · {esc(full_report_date)}" if full_report_date else ""}</span></h2>
  <div class="legend">
    <span class="sub">年报/半年报披露的全部股票持仓（标的+占比，不含行情）· 季报只披露前十大，故不纳入本档案 · 期次按法定披露时限自动切换</span>
  </div>
  {full_block}
</div>

<footer class="sub" style="text-align:center;margin:24px 0 8px;">
  qdii-nav-tracker · 数据源：天天基金F10 / 东财lsjz / 中行牌价 / 新浪行情 · 仅供研究，非投资建议
</footer>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
var US_FULL = {json.dumps(chart_full, ensure_ascii=False)};
var US_PERIOD = {json.dumps(chart_period, ensure_ascii=False)};
var US_TOPPER = {json.dumps(chart_topper, ensure_ascii=False)};
var US_KIND = {json.dumps(chart_kind, ensure_ascii=False)};
var US_COV = {json.dumps(chart_cov)};
var US_TOP = {json.dumps(chart_us)};
var US_BETA = {json.dumps(chart_beta)};
new Chart(document.getElementById('usChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(chart_labels, ensure_ascii=False)},
    datasets: [
      {{
        label: '估算美股含量（结构外推）',
        data: {json.dumps(chart_est)},
        backgroundColor: '#2563eb', borderRadius: 4,
        barPercentage: 0.62, categoryPercentage: 0.62
      }},
      {{
        label: '当期披露（下限）',
        data: {json.dumps(chart_us)},
        type: 'line',
        borderColor: '#f59e0b', backgroundColor: '#f59e0b',
        borderWidth: 2, pointRadius: 3, pointHoverRadius: 5,
        pointStyle: 'circle', tension: 0.2, fill: false
      }}
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{mode: 'index', intersect: false}},
    plugins: {{
      legend: {{position: 'bottom', labels: {{boxWidth: 10, padding: 10, font: {{size: 11}}, usePointStyle: true}}}},
      tooltip: {{
        callbacks: {{
          label: function(ctx) {{
            return ' ' + ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + '%';
          }},
          afterLabel: function(ctx) {{
            var i = ctx.dataIndex;
            if (ctx.datasetIndex === 0) {{
              var d = (ctx.parsed.y - US_TOP[i]).toFixed(1);
              return ['= 当期披露 ' + US_TOP[i] + '%（' + US_TOPPER[i] + '）',
                      '＋ 未覆盖仓位外推 ' + d + 'pp',
                      '外推结构取自 ' + US_PERIOD[i] + ' 全持仓（美股 ' + US_FULL[i] + '%）',
                      'NDX β ' + US_BETA[i]];
            }}
            return ['期次 ' + US_TOPPER[i] + ' · ' + US_KIND[i],
                    '覆盖净值仓位 ' + US_COV[i] + '%',
                    '季报只披露前十大 → 这是下限，不是真实敞口'];
          }}
        }}
      }}
    }},
    scales: {{
      y: {{beginAtZero: true, max: 100, ticks: {{callback: v => v + '%'}}, grid: {{color: 'rgba(128,128,128,.12)'}}}},
      x: {{grid: {{display: false}}, ticks: {{font: {{size: 11}}}}}}
    }}
  }}
}});

new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: {trend_dates_json},
    datasets: {json.dumps(trend_sets, ensure_ascii=False)}
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{mode: 'index', intersect: false}},
    plugins: {{
      legend: {{position: 'bottom', labels: {{boxWidth: 10, padding: 8, font: {{size: 10}}, usePointStyle: true}}}},
      tooltip: {{
        displayColors: true,
        callbacks: {{
          label: function(ctx) {{
            var v = ctx.parsed.y;
            return ' ' + ctx.dataset.label + ': ' + (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
          }}
        }}
      }}
    }},
    scales: {{
      y: {{ticks: {{callback: v => (v >= 0 ? '+' : '') + v + '%'}}, grid: {{color: 'rgba(128,128,128,.12)'}}}},
      x: {{grid: {{display: false}}, ticks: {{maxTicksLimit: 6, font: {{size: 10}}}}}}
    }}
  }}
}});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    print("HTML ->", out_path, f"({len(html_doc)} bytes)")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--history", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--holdings-full", default="", help="output/holdings_full.json（全部持仓档案，年报/半年报口径，可选）")
    a = ap.parse_args()
    with open(a.json, encoding="utf-8") as f:
        report = json.load(f)
    history = load_history(a.history) if a.history else []
    full_h = None
    if a.holdings_full and os.path.exists(a.holdings_full):
        with open(a.holdings_full, encoding="utf-8") as f:
            full_h = json.load(f)
    render(report, history, a.out, full_h)

if __name__ == "__main__":
    main()
