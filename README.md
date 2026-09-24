# QDII Nav Tracker — 场外 QDII 基金持仓跟踪与净值预测

用**二十大持仓披露 + 全球股市日行情**预测场外 QDII 基金每日净值涨跌（披露覆盖率 48%→69%），验证持仓真实性，反推美股含量，并通过**滚动 NNLS 动态权重**追踪基金调仓。

每日北京时间 06:00 自动运行（仅美股交易日盘后，对齐 portfolio 仓），由 Cloudflare Worker `qdii-dispatch` 定时调用 GitHub `workflow_dispatch` 触发（绕开 GitHub Actions schedule 共享队列偶发漏触发），产出报告到 `output/`，并渲染静态网页到 `docs/`（GitHub Pages 发布）。

## 核心方法

### 时间对齐规则（经实测验证）
- 净值日期 D 对应全球市场 **「交易日 ≤ D」最新收盘**（lag=0）
- 美东 T 日收盘 = 北京 T+1 日凌晨 → 北京 T+1 日晚公布净值（日期 T+1）
- 港股（北京 16:00 收）、A股（15:00 收）、日股/韩股（北京 8:00 开、14:30 收）均**当天收盘即计入当晚净值**（实测 corr 0.912 vs 前一日 0.500）
- 因此北京 06:00 运行时，可用美股 T 日收盘 + 日韩港 T 日收盘预测**今晚将公布**的净值涨跌

### 三通道分析（每只基金）
| 通道 | 方法 | 回答的问题 |
|---|---|---|
| 静态披露权重 | 二十大持仓占比 × 各市场日收益（美股/港股/A股/日韩，含 USDCNH 折算） | 披露持仓能否解释净值？（R²/方向一致率） |
| 滚动 NNLS 动态权重 | walk-forward 60日非负最小二乘 | 实际持仓与披露差多少？（疑似调仓） |
| 美股指数暴露 | 净值对 NDX 回归（近6月 β） | 实际美股含量多大？（NDX β） |

> **全市场参与预测**：美股/港股/A股（主板+创业板+科创板）/日韩股全部纳入静态权重篮子。净值日定价基准 = 美股 T 日凌晨收盘（美东 T-1）+ 日韩/港股/A股 T 日白天收盘，预测输入与基金公司估值完全对齐。

### 前瞻预测 + 自动验证闭环
- **每晚预测**：用美股最近收盘（统一 us_last 基准）预测当晚将公布的净值涨跌（静态披露权重 + 滚动NNLS 双预测），全部基金共用同一预测净值日，不因净值更新节奏不同而分裂
- **预测/验证分流**：基金最新净值日期 ≥ us_last → 该期已公布，走验证（显示预测 vs 实际对照）；< us_last → 生成新预测（待公布）
- **自动验证**：预测写入 `output/predictions.jsonl`，净值公布后自动回填 actual，统计方向命中率 / MAE，网页展示"历史预测验证"板块
- **方向背离提示**：预测方向与 NDX 指数当日收益相反时标注"⚠️与大盘背离"（卡片右上角显示 NDX 当日收盘点位+涨跌幅，方便对照）

### 实测结论（021842 国富全球科技C）
- 静态披露权重预测：**方向准确率 95.5%**，MAE 1.08pp，相关性 0.943
- 滚动60日 NNLS：MAE 降至 **0.69pp**（幅度精度提升 36%），方向 95.5% 持平
- 披露美股占比 49.8%，但 NDX β=1.83 → **实际美股敞口高于披露**（前二十大之外仍大量持有美股）
- 最新 NNLS 权重显示 TSM/GOOG/MU 实际持仓显著高于披露，LRCX 疑似清仓

### 精度评估体系（五维）
| 指标 | 定义 |
|---|---|
| 方向命中率 | sign(预测) == sign(实际) |
| **幅度命中率 ±1pp** | \|误差\| ≤ 1.0pp |
| **综合精度命中** | 方向对 且 \|误差\|≤1.0pp |
| **区间覆盖率** | 实际 ∈ 预测 ± 1.28×MAE 误差带（80%置信） |
| MAE / RMSE / bias | 误差统计（bias=平均误差，正=高估，供校准依据） |

- **幅度命中率**比方向命中率更能反映预测精度（方向 92% 但幅度 61% 是常态，方向命中"不难"）
- **统一命中规则**：所有 ✓/✗（验证表格、预测卡片）均为**综合命中**——方向对 且 |误差|≤1.0pp 才算 ✓，方向对但幅度差远显示 ✗（避免"猜对涨跌"掩盖"幅度不准"）

## 目录结构

```
├── .github/workflows/qdii-daily.yml   # 由 Cloudflare Worker qdii-dispatch 触发（北京 06:00 · 仅美股交易日）
├── config/funds.json      # 基金清单（增减基金改这里，下次 cron 自动生效）
├── scripts/
│   ├── data_fetcher.py    # 数据获取（F10持仓/净值/美股/港股/A股/日韩股/汇率/指数/缺口补齐）
│   ├── analysis.py        # 核心分析（静态/滚动NNLS/指数暴露/前瞻预测）
│   ├── run_daily.py       # 每日主入口（预测+验证+历史记录）
│   └── render_html.py     # 渲染静态网页 docs/index.html
├── output/                # 每日报告（daily_report.json + summary.md + predictions.jsonl + holdings_cache.json）
├── docs/                  # 静态网页（GitHub Pages 发布）
├── requirements.txt
└── README.md
```

## 本地运行

```bash
pip install -r requirements.txt
python scripts/run_daily.py --out output        # 正常（交易日判断）
python scripts/run_daily.py --force --out output  # 强制运行（测试）
python scripts/render_html.py --json output/daily_report.json --history output/predictions.jsonl --holdings-full output/holdings_full.json --out docs/index.html
```

## 静态网页

GitHub Pages 发布（main 分支 /docs 目录）

页面区块：⭐今晚净值预测（12 基金卡片，右上角 NDX 当日收盘徽章；待公布显示预测、已公布显示预测vs实际对照）→ 历史预测验证（五维指标/对照表/查看更多）→ **基金涨跌幅对比（近30日折线图，相对首日累计涨跌%，颜色自动分配不重复）** → 美股含量总览（Chart.js 条形图）→ 持仓质量表 → 疑似调仓（全部二十大持仓+中文名+**最新涨跌幅列**）→ **全部半年报持仓**（静态披露档案：中报/年报全量标的+占比，可折叠展开）

## 支持市场与数据源

| 数据 | 主源 | 备源/兜底 |
|---|---|---|
| 二十大持仓 / 全持仓档案 | 天天基金 F10（HTTP/1.1 直连） | 二十大缓存 holdings_cache.json（90 天）+ 全持仓档案 holdings_full.json（披露期 120 天） |
| 基金净值 | **akshare 东财（首选，云端 lsjz 被东财限流）** | 东财 f10/lsjz 直连 |
| **申购限额** | 东财 fund_purchase_em（akshare，仅金额/暂停） | 失败返回 {} 不影响主流程 |
| 美股日线 | **yfinance（首选，含当天实时；6mo 精简）** | akshare 新浪 → 腾讯 qt.gtimg.cn 实时快照（当日兜底） |
| 港股 / A股日线 | akshare（新浪源） | 腾讯 qt.gtimg.cn 实时快照（当日预测兜底） |
| 日股 / 韩股 | **yfinance（.T/.KS，首选；6mo 精简）** | 东财 push2his（JP=176/KR=177 secid，tail200）→ 腾讯快照（kr/jp 前缀，当日） |
| USD/CNH 汇率 | 中行牌价 currency_boc_safe | 东财 push2his（curl_cffi）→ yfinance |
| NDX / INX 指数 | **yfinance（^NDX / ^GSPC）** | akshare 新浪 |
| **指数当日涨跌幅（页面徽章）** | **腾讯行情自带涨跌幅（usNDX）** | 东财 push2his（100.NDX100 / 100.SPX）→ 新浪 → 已通过相邻交易日校验的 yfinance 日线 |
| 美股交易日历 | pandas-market-calendars（NYSE） | weekday 近似 |
| 中国基金净值日历 | pandas-market-calendars（XSHG） | QDII 净值只在 A股交易日公布：中国节假日（工作日但 A股休市，国庆/春节等）→ 当日跳过；长假回归首日（最新净值 ↔ 美股最近收盘间隔 ≥2 个美股交易日）→ 跳过当日预测（净值将一次反映多日美股累计，单日模型必大偏差），次日自动恢复 |
| **交易日缺口补齐** | 新浪日线（云端最稳） | 东财 push2his（curl_cffi）→ 腾讯快照（仅最近一日）；同一源连续 3 次补不到即**熔断**（本次运行不再尝试）；全失败 → NaN 占位 + 该基金当日跳过预测并告警 |

> **A股全市场识别**：`classify_market` 6 位数字统一归 CN（主板 000/600/601/603/605 + 创业板 300 + 科创板 688）——曾只认 3 开头，主板/科创板持仓（如 600183/603986/688498）被误判 SKIP 不参与预测。**日韩股亦已纳入静态权重篮子参与预测**（此前是预测盲区）。

> **数据源可观测性**：每次数据查询的日志都标注数据源与结果（`✓ [美股MU/yf] 成功 (N条)` / `✗ 失败: 原因` / `⚠ 空数据`），运行末尾打印「数据源汇总」一行，一眼确认每个源是否生效、是否降级。

> **数据量精简**：美股/日韩/NDX 均拉近 6 个月（~130 交易日，足够 60 日 NNLS + 近6月 β 回归），东财日韩兜底 tail(200)；`_CACHE` 跨基金去重（美股每只仅查 1 次，12 只基金共享）。

### 数据质量与交易日缺口补齐

`data_fetcher._repair_missing_days()` 对**以 yfinance 为主源的市场（美股/日韩）**做交易日完整性校验：

1. 用市场日历（NYSE/JPX/XKRX）算出序列覆盖区间内应有而缺失的交易日，以及值为 NaN 的空行；
2. 按 **新浪 → 东财 → 腾讯快照** 顺序补齐真实收盘（同一源只请求一次，批量补多天）；
3. **源级熔断**：同一源连续 3 次补不到 → 本次运行不再尝试该源（写入报告 `data_quality.sources_disabled`）；
4. 补不到 → 以 **NaN 占位**（保留日期），并登记 `unresolved`；
5. `analysis.predict_next()` 在预测前检查依赖标的近 4 天是否有未修复缺口 → 有则**跳过该基金当日预测并告警**（宁可不出预测，也不出错预测）。

报告新增 `data_quality` 字段（`counts` / `repaired` / `unresolved` / `sources_disabled`），运行日志与页面徽章（⚠️ 数据缺口 N）同步暴露。

> **源顺序为何是"新浪优先"**：云端实测 新浪补缺口 11/11 成功，东财 push2his 仅 11/46（GitHub runner 出口 IP 被限流）。
> 把东财放首位会让每次失败都带重试+休眠，实测把分析步骤从 ~153s 拖到 ~479s。两者补的都是同一交易日的真实收盘，**互换顺序不改变结果**。

> **港股/A股不做补齐**：主源是新浪，历史完整，不引入改动风险。

> **净值（NAV）进程内缓存**：同一只基金的净值在一次运行里被 4 处调用（探针/分析/30日走势/摘要），
> 实测 12 只共 37 次网络请求（仅需 12 次）。`get_nav` 现按 code 缓存、返回副本 → 降到 12 次，结果不变。

### 基金清单与特殊类型

> **基金清单配置化**：`config/funds.json` 驱动（可经本地维护面板 github-data-maintainer.html 编辑，或直接改文件提交）。run_daily 的 `load_funds_config()` + render_html 的 `_load_fund_names()` 读取，缺失回退内置默认。**增减基金 → 下次 cron 自动生效**（卡片/走势图/验证/涨跌幅列全自动跟随）。当前 12 只：002891/008254/014002/015202/016702/018147/021277/021842/022184/080006/024239/017093。

> **持仓代理 `holdings_proxy`**：ETF 联接基金 / FOF 的 F10「股票投资明细」可能长期停更（真实持仓是持有的目标 ETF 份额）。在 config 里给该类基金加 `"holdings_proxy": "<目标ETF代码>"`，分析时**持仓取目标 ETF（穿透披露）、净值仍用基金自身**。实例：`017093 景顺纳科C → 159509`（景顺长城纳斯达克科技市值加权 ETF）。实测该基金静态/滚动回测 MAE 0.26 / 0.24pp，优于现有基金均值。

> **持仓期次校验**：解析 F10 最新报告期，若比「当前应已披露期」落后 >2 个季度 → 判定数据过期，**跳过该基金预测并告警**（防止持仓停更的基金静默产出错误预测并污染命中率统计）。

> **走势图颜色自动分配**：预定义 12 色基础调色板 + 4 只固定色（华宝=黑/易方达=橙/长盛=深青/华夏科技=棕褐）+ 按 code 排序补色 + 超限哈希色相兜底——新增基金自动拿到不重复颜色，零维护。

> 多源降级链参考 portfolio（净值/汇率）、douban-tracker（腾讯行情）、cmb-tracker（fallback_chain）生产验证经验。

## 已覆盖的日韩股持仓

| 代码 | 名称 | 市场 | 东财 secid |
|---|---|---|---|
| 285A | KIOXIA 铠侠 | JP | 176.285A |
| 6857 | 爱德万测试 | JP | 176.6857 |
| 005930 | 三星电子 | KR | 177.005930 |
| 000660 | SK海力士 | KR | 177.000660 |

## 已知边界

- 二十大持仓覆盖基金仓位约 55~85%，剩余仓位影响会分摊进 NNLS 权重（看趋势、不直接当精确占比）
- 披露为季末时点（6/30），7/1 后调仓会降低静态 R²；半年报披露后可升级全持仓口径
- 幅度预测（MAE ~0.5-1.1pp）只能参考，方向预测（77~95%）更可靠；方向偏差源于"披露持仓≠实际持仓"，NNLS 能缩小但无法完全消除
- 腾讯快照兜底在"日韩已收盘+美股未收盘"窗口（北京 9:00-16:00）手动触发时可能跳过日韩股（已知边界，暂不处理）
- 数据源（尤其 Yahoo）出现「整行空值」的 K 线时，若东财/新浪/腾讯都补不到该交易日 → 相关基金当日**不出预测**（宁缺勿错），页面显示"⚠️ 数据缺口 N"

---

## 排错与踩坑记录

### 1. Yahoo 整行空值 K 线 → yfinance 静默丢行 → 交易日缺口

**症状**：页面 NDX 徽章显示 `30,470 ▼ -0.04%`，实际当日应为 **-0.85%**（点位正确、涨跌幅错误）。

**根因**：Yahoo 对某个交易日返回**整行空值**的日K（`O/H/L/C/V` 全为 `null`），而 yfinance `history()` 默认
`keepna=False` 会把这类行**静默丢弃**（源码：`(df[cols].isna() | (df[cols]==0)).all(axis=1)` → drop）→ 序列出现
**交易日缺口** → 任何"相邻两行 = 相邻交易日"的假设（`close[-1]/close[-2]`、`pct_change`）都会**把多日累计当单日**。

诊断证据：Yahoo 原始 API 返回 **127 条**，yfinance 返回 **126 条**，生产日志 `✓ [yf指数 .NDX] 成功 (126条)`；
反推 `prev_close=30482.35` 正是缺口前一日（9/21）的收盘。同一根空行也出现在 **ASML** 上（影响 9 只基金持仓）。

**修复**：
1. 所有 yfinance 调用统一走 `_yf_history()`，传 `keepna=True` 保留空行（老版本无此参数时自动降级）；
2. `_repair_missing_days()` 按 **东财 → 新浪 → 腾讯快照** 补齐；全补不到 → NaN 占位 + 跳过该基金当日预测；
3. 页面 NDX 涨跌幅改为**直接取行情接口自带值**（不再由日线序列相减）；
4. `asof_ret()` 不再预先 `dropna`，让缺口日命中即返回 `NaN`（旧实现会静默回退到更早一个交易日，返回"看起来正常但不对应目标日"的值）。

### 2. 东财 secid 地雷：`100.NDX` 不是纳斯达克100

`100.NDX` = **纳斯达克综合指数**；**纳斯达克100 必须用 `100.NDX100`**。标普500 = `100.SPX`、道指 = `100.DJIA`。
用错会得到一个"看起来很合理"的完全错误点位。

### 3. 东财 push2his 需 curl_cffi，且不能走代理

境内源直连：用 `requests` 会被 `RemoteDisconnected`，必须用 `curl_cffi`（`impersonate="chrome"`）；
同时**不要给它挂代理**（走代理必然失败）。

### 4. 本地 yfinance 常被限流 → 诊断用 Yahoo chart API 直连

家宽 IP 调 yfinance 常报 `YFRateLimitError`，云端 Actions IP 正常。本地排查数据问题时改用
`https://query1.finance.yahoo.com/v8/finance/chart/<symbol>?range=6mo&interval=1d`（需代理）可直接拿到
原始 `meta.previousClose` / 每根 K 线，绕开 yfinance 库的限流与加工。

### 5. CI 用 Python 3.11，f-string 不能嵌套同类引号

`f"{d['k']}"` 合法，但 `f"{[f'{g['symbol']}' for g in x]}"` 在 3.11 直接 `SyntaxError`
（PEP 701 才放宽，3.12+）。写嵌套表达式前先赋给临时变量。

### 6. 给 CI 加"网络兜底链"会显著拖慢 Actions —— 必须配熔断

新增缺口补齐后，分析步骤从 **153s 涨到 479s（3.1 倍）**。原因是 GitHub runner 出口 IP 对东财
push2his 大量限流（实测 11 成功 / 35 失败），而每次失败都带 2 次重试 + 休眠，且每只美股还要
依次试 3 个市场前缀（105/106/107）。

**教训**：往 CI 里加任何"失败再换源"的链，都必须同时加 **源级熔断**（连续 N 次补不到就本次运行不再尝试）
+ **优先放云端稳定的源**。**加机制前先量一次耗时**，否则功能对了、额度与时间白烧。

### 7. run_daily 在报告写完之后抛异常 → workflow 的「Commit outputs」被跳过 → 结果全丢

`run_daily.py` 先写 `daily_report.json` / `summary.md`，再打印数据质量汇总。
一次改动里汇总打印遍历了 `results`（**它是 dict，不是 list**，`for x in results` 拿到的是字符串键）
→ `AttributeError` → step 退出码 1 → 后续 `Render static HTML` 与 `Commit outputs` **全部被跳过**，
报告写在了 runner 临时盘上、什么都没提交。日志里还能看到 `DONE -> output/daily_report.json`，
极易误判为"跑成功了"。

**教训**：报告落盘之后的任何收尾代码（打印、统计、告警）都必须 `try/except` 包住——
它不该有能力让整次运行前功尽弃。判断 Action 是否真正成功，要**看步骤结论，而不是看日志里的 DONE**。

### 8. GitHub API 403 `Request forbidden by administrative rules`（缺 User-Agent）

Cloudflare Worker 调 `api.github.com` 时，GitHub REST API **强制要求 `User-Agent` header**，
缺失会被 403 拒绝（错误信息藏在响应体里，光看状态码很容易误判成 token 权限问题）。

**症状**：`/trigger` 返回 `dispatched status=403`（HTTP 整体 200，但业务状态是 403）。

**根因**：worker.js 的 `fetch` 没带 `User-Agent` header（Cloudflare Worker 默认不补浏览器 UA）。
与 GITHUB_TOKEN 无关——token 无效通常是 401，不是 403。

**修复（worker.js headers 加一行）**：
```js
headers: {
  "User-Agent": "qdii-dispatch-worker",   // ← 缺这个，GitHub 直接 403
  Authorization: `Bearer ${token}`,
  ...
}
```

**排查技巧**：开发时让 worker 把 GitHub 响应体透传回来（而非只返回状态码），
一眼就能看到真实原因。诊断外部 API 报错，**先透传完整响应体，别只看状态码猜**。
