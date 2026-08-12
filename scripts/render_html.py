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

FUND_NAMES = {"002891": "华夏移动互联", "008254": "华宝致远C", "014002": "浦银全球智能C",
              "015202": "汇添富全球移动C", "016702": "银华海外数字C", "018147": "建信新兴C",
              "021277": "广发全球精选C", "021842": "国富全球科技C"}

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
.badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;background:#eef2ff;color:var(--accent);}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;}
.fund-card{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:#fafbfe;}
.fund-card .fname{font-size:13px;font-weight:600;color:var(--ink);}
.fund-card .fcode{font-size:11px;color:var(--sub);margin-top:1px;}
.fund-card .pred{font-size:26px;font-weight:800;margin:8px 0 2px;letter-spacing:-.5px;}
.fund-card .meta{font-size:12px;color:var(--sub);display:flex;justify-content:space-between;margin-top:6px;padding-top:8px;border-top:1px dashed var(--line);}
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
    if v is None:
        return "-"
    return f"{v:.{digits}f}pp"

def pred_color(v):
    if v is None:
        return ""
    return "up" if v > 0 else ("down" if v < 0 else "")

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

def render(report, history, out_path):
    date = report.get("date", "")
    gen = report.get("generated_at", "")
    funds = report.get("funds", {})
    verify = report.get("verify", {})
    hist_verified = [h for h in history if h.get("actual") is not None]

    # ---- 今晚预测卡片（保留全部基金：待验证显示预测，已公布显示预测vs实际）----
    pred_cards = []
    for code, r in funds.items():
        if "error" in r:
            continue
        p = r.get("predict")
        if p is not None:
            # 有预测：待验证
            pn = p.get("pred_nnls")
            pred_cards.append({
                "code": code, "name": FUND_NAMES.get(code, code),
                "pred": p["pred_static"], "pred_nnls": pn,
                "pred_nav": p["pred_nav_static"], "last_nav": p["last_nav"],
                "next_date": str(p["next_date"])[:10],
                "status": "pending", "actual": None, "hit": None, "err": None,
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
                    "err": rec.get("err"),
                })
    pred_cards.sort(key=lambda x: (0 if x["status"] == "pending" else 1, -x["pred"]))

    cards_html = []
    for c in pred_cards:
        cls = pred_color(c["pred"])
        arrow = "▲" if c["pred"] > 0 else ("▼" if c["pred"] < 0 else "—")
        nnls_s = fmt_pct(c["pred_nnls"]) if c["pred_nnls"] is not None else "-"
        if c["status"] == "pending":
            status_badge = '<span class="badge">待公布</span>'
            actual_html = f'<div class="meta"><span>最新净值 {c["last_nav"]:.4f}</span><span>{esc(c["next_date"])}公布</span></div>'
        else:
            hit = c.get("hit")
            mark = '<span class="ok">✓</span>' if hit else '<span class="no">✗</span>'
            status_badge = f'<span class="badge" style="background:#f0fdf4;color:#166534">已公布 {mark}</span>'
            actual_html = (f'<div class="meta"><span>实际 {fmt_pct(c["actual"])}</span>'
                           f'<span>误差 {fmt_pp(c["err"])}</span></div>'
                           f'<div class="meta"><span>实际净值 {c["last_nav"]:.4f}</span><span>{esc(c["next_date"])}公布</span></div>')
        cards_html.append(f"""
        <div class="fund-card">
          <div class="fname">{esc(c['name'])} <span class="fcode">{c['code']}</span> {status_badge}</div>
          <div class="pred {cls}">{arrow} {fmt_pct(c['pred'])}</div>
          <div class="meta"><span>滚动NNLS {nnls_s}</span><span>预测净值<br/><b>{c['pred_nav']:.4f}</b></span></div>
          {actual_html}
        </div>""")
    pred_block = "\n".join(cards_html) if cards_html else "<p class='sub'>暂无预测数据</p>"

    # ---- 历史验证 ----
    v_n = verify.get("n", 0)
    v_dir = verify.get("dir_acc")
    v_mae = verify.get("mae")
    v_recent = verify.get("recent", [])[::-1]  # 最新在前
    v_rows = []
    for v in v_recent:
        hit = v.get("hit")
        mark = '<span class="ok">✓</span>' if hit else '<span class="no">✗</span>'
        v_rows.append(f"<tr><td>{FUND_NAMES.get(v['code'], v['code'])}</td><td>{esc(v['pred_date'])}</td>"
                      f"<td>{fmt_pct(v['pred_static'])}</td><td>{fmt_pct(v.get('actual'))}</td>"
                      f"<td>{mark}</td><td>{fmt_pp(v.get('err'))}</td></tr>")
    v_table = ("<table><thead><tr><th>基金</th><th>净值日</th><th>预测</th><th>实际</th><th>命中</th><th>误差</th></tr></thead>"
               f"<tbody>{''.join(v_rows)}</tbody></table>") if v_rows else "<p class='sub'>暂无已公布验证记录，明日起自动累积</p>"

    # ---- 美股含量图表数据 ----
    chart_labels, chart_us, chart_beta = [], [], []
    for code in ["014002", "002891", "021842", "008254", "016702", "018147", "015202", "021277"]:
        r = funds.get(code)
        if not r or "error" in r:
            continue
        chart_labels.append(f"{FUND_NAMES.get(code, code)}")
        chart_us.append(r.get("us_pct", 0))
        chart_beta.append(round((r.get("ndx_beta") or {}).get("ndx_beta", 0), 2))

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
        q_rows.append(f"<tr><td>{FUND_NAMES.get(code, code)}<br/><span class='sub'>{code}</span></td>"
                      f"<td>{r.get('us_pct', 0):.1f}%</td>"
                      f"<td>{beta:.2f}</td>"
                      f"<td>{s.get('dir_acc', 0):.1f}%</td>"
                      f"<td>{s.get('mae', 0):.2f}pp</td>"
                      f"<td>{rr.get('mae', 0):.2f}pp</td>"
                      f"<td>{s.get('r2', 0):.3f}</td></tr>")
    q_table = ("<table><thead><tr><th>基金</th><th>披露美股%</th><th>NDX β</th><th>方向准确率</th>"
               "<th>静态MAE</th><th>滚动MAE</th><th>持仓R²</th></tr></thead>"
               f"<tbody>{''.join(q_rows)}</tbody></table>")

    # ---- 疑似调仓折叠 ----
    adj_details = []
    for code, r in funds.items():
        if "error" in r or not r.get("nnls_weight"):
            continue
        w = r["nnls_weight"]
        disc = {x["code"]: x["pct"] for x in r.get("holdings", [])}
        rows = []
        for c, wt in sorted(w.items(), key=lambda kv: kv[1], reverse=True):
            if c == "FX":
                continue
            d = disc.get(c, 0)
            diff = wt * 100 - d
            flag = '<span class="ok">▲加仓</span>' if diff > 2 else ('<span class="no">▼减仓</span>' if diff < -2 else "")
            rows.append(f"<tr><td>{esc(c)}</td><td>{d:.1f}%</td><td>{wt*100:.1f}%</td>"
                        f"<td>{diff:+.1f}% {flag}</td></tr>")
        adj_details.append(f"""
        <details>
          <summary>{FUND_NAMES.get(code, code)}（{code}）— 疑似调仓 {sum(1 for v in w.values() if v != 0)} 项</summary>
          <table style="margin-top:8px"><thead><tr><th>代码</th><th>披露%</th><th>NNLS估计%</th><th>差异</th></tr></thead>
          <tbody>{''.join(rows)}</tbody></table>
        </details>""")
    adj_block = "\n".join(adj_details) if adj_details else "<p class='sub'>暂无数据</p>"

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
  <div><h1>QDII 净值跟踪</h1><div class="sub">十大持仓 × 美股行情 · 静态 + 滚动NNLS</div></div>
  <div class="sub">报告日 {esc(date)}<br>更新 {esc(gen)}</div>
</header>

<div class="card">
  <h2>今晚净值预测 <span class="badge">{esc(date)}</span></h2>
  <div class="legend">
    <span><i style="background:var(--up)"></i>预测上涨</span>
    <span><i style="background:var(--down)"></i>预测下跌</span>
    <span style="display:flex;align-items:center;gap:5px;"><span style="font-size:11px;padding:1px 6px;border-radius:99px;background:#f0fdf4;color:#166534">已公布</span>已公布净值的显示预测 vs 实际对照</span>
    <span class="sub">静态=披露权重 · 滚动=60日NNLS · 基于美股最新收盘（lag=0）</span>
  </div>
  <div class="grid">{pred_block}</div>
</div>

<div class="card">
  <h2>历史预测验证 <span class="badge">{v_n} 条已公布</span></h2>
  <div class="stat-grid">
    <div class="stat"><div class="num">{f"{v_dir:.1f}%" if v_dir is not None else "-"}</div><div class="lbl">方向命中率</div></div>
    <div class="stat"><div class="num">{f"{v_mae:.2f}" if v_mae is not None else "-"}</div><div class="lbl">MAE（pp）</div></div>
    <div class="stat"><div class="num">{sum(1 for h in hist_verified if h.get("hit"))}/{len(hist_verified) if hist_verified else 0}</div><div class="lbl">累计命中</div></div>
    <div class="stat"><div class="num">{len([h for h in history if h.get("actual") is None])}</div><div class="lbl">待验证</div></div>
  </div>
  {v_table}
</div>

<div class="card">
  <h2>美股含量总览</h2>
  <div class="legend">
    <span><i style="background:var(--accent)"></i>披露美股占比 %（十大）</span>
    <span class="sub">NDX β 见右侧标签，>1 表示对纳指放大暴露</span>
  </div>
  <div class="chart-box"><canvas id="usChart" role="img" aria-label="美股含量对比条形图">美股含量对比</canvas></div>
</div>

<div class="card">
  <h2>持仓质量（历史回测）</h2>
  {q_table}
</div>

<div class="card">
  <h2>疑似调仓（滚动NNLS vs 披露）</h2>
  {adj_block}
</div>

<footer class="sub" style="text-align:center;margin:24px 0 8px;">
  qdii-nav-tracker · 数据源：天天基金F10 / 东财lsjz / 中行牌价 / 新浪行情 · 仅供研究，非投资建议
</footer>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
new Chart(document.getElementById('usChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(chart_labels, ensure_ascii=False)},
    datasets: [{{
      label: '披露美股占比',
      data: {json.dumps(chart_us)},
      backgroundColor: '#2563eb', borderRadius: 4,
      barPercentage: 0.6, categoryPercentage: 0.5
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{display: false}},
      tooltip: {{
        callbacks: {{
          afterLabel: function(ctx) {{
            return 'NDX β: ' + {json.dumps(chart_beta)}[ctx.dataIndex];
          }}
        }}
      }}
    }},
    scales: {{
      y: {{beginAtZero: true, ticks: {{callback: v => v + '%'}}, grid: {{color: 'rgba(128,128,128,.12)'}}}},
      x: {{grid: {{display: false}}, ticks: {{font: {{size: 11}}}}}}
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
    a = ap.parse_args()
    with open(a.json, encoding="utf-8") as f:
        report = json.load(f)
    history = load_history(a.history) if a.history else []
    render(report, history, a.out)

if __name__ == "__main__":
    main()
