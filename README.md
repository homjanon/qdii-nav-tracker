# QDII Nav Tracker — 场外 QDII 基金持仓跟踪与净值预测



用**十大持仓披露 + 全球股市日行情**预测场外 QDII 基金每日净值涨跌，验证持仓真实性，反推美股含量，并通过**滚动 NNLS 动态权重**追踪基金调仓。



GitHub Actions 每日北京时间 06:00 自动运行（仅美股交易日盘后，对齐 portfolio 仓，叠加调度延迟实际约 06:30~07:00 完成），产出报告到 `output/`，并渲染静态网页到 `docs/`（GitHub Pages 发布）。



## 核心方法



### 时间对齐规则（经 021842 / 021277 实测验证）

- 净值日期 D 对应全球市场 **「交易日 ≤ D」最新收盘**（lag=0）

- 美东 T 日收盘 = 北京 T+1 日凌晨 → 北京 T+1 日晚公布净值（日期 T+1）

- 港股（北京 16:00 收）、A股（15:00 收）、日股/韩股（北京 8:00 开、14:30 收）均**当天收盘即计入当晚净值**（实测 corr 0.912 vs 前一日 0.500）

- 因此北京 06:00 运行时，可用美股 T 日收盘 + 日韩港 T 日收盘预测**今晚将公布**的净值涨跌



### 三通道分析（每只基金）

| 通道 | 方法 | 回答的问题 |

|---|---|---|

| 静态披露权重 | 十大持仓占比 × 美股日收益（含 USDCNH 折算） | 披露持仓能否解释净值？（R²/方向一致率） |

| 滚动 NNLS 动态权重 | walk-forward 60日非负最小二乘 | 实际持仓与披露差多少？（疑似调仓） |

| 美股指数暴露 | 净值对 NDX 回归（近6月 β） | 实际美股含量多大？（NDX β） |



### 前瞻预测 + 自动验证闭环

- **每晚预测**：用美股最近收盘（统一 us_last 基准）预测当晚将公布的净值涨跌（静态披露权重 + 滚动NNLS 双预测），全部基金共用同一预测净值日，不因净值更新节奏不同而分裂

- **预测/验证分流**：基金最新净值日期 ≥ us_last → 该期已公布，走验证（显示预测 vs 实际对照）；< us_last → 生成新预测（待公布）

- **自动验证**：预测写入 `output/predictions.jsonl`，净值公布后自动回填 actual，统计方向命中率 / MAE，网页展示"历史预测验证"板块

- **方向背离提示**：预测方向与 NDX 指数当日收益相反时标注"⚠️与大盘背离"



### 实测结论（021842 国富全球科技C，2026-08-11）

- 静态披露权重预测：**方向准确率 95.5%**，MAE 1.08pp，相关性 0.943

- 滚动60日 NNLS：MAE 降至 **0.69pp**（幅度精度提升 36%），方向 95.5% 持平

- 披露美股占比 49.8%，但 NDX β=1.83 → **实际美股敞口高于披露**（十大之外仍大量持有美股）

- 最新 NNLS 权重显示 TSM/GOOG/MU 实际持仓显著高于披露，LRCX 疑似清仓

- 验证 13 条：方向命中率 76.9%，MAE 0.54pp



## 目录结构



```

├── .github/workflows/qdii-daily.yml   # 每日自动运行（北京 06:00 · 仅美股交易日）

├── config/

│   └── funds.json                     # ⭐ 基金清单（新增/移除基金在此维护，见下）

├── scripts/

│   ├── data_fetcher.py    # 数据获取（F10持仓/净值/美股/港股/A股/日韩股/汇率/指数）

│   ├── analysis.py        # 核心分析（静态/滚动NNLS/指数暴露/前瞻预测）

│   ├── run_daily.py       # 每日主入口（预测+验证+历史记录）

│   └── render_html.py     # 渲染静态网页 docs/index.html

├── output/                # 每日报告（daily_report.json + summary.md + predictions.jsonl + holdings_cache.json）

├── docs/                  # 静态网页（GitHub Pages 发布）

├── requirements.txt

└── README.md

```



## 基金清单维护（config/funds.json）



跟踪的场外 QDII 基金清单统一在 **`config/funds.json`** 维护（2026-08-16 起由代码内硬编码改为外部配置），`run_daily.py` 与 `render_html.py` 均读取该文件，**新增/移除基金无需改任何代码**：



```json

{

  "funds": [

    {"code": "002891", "name": "华夏移动互联"},

    {"code": "021842", "name": "国富全球科技C"}

  ]

}

```



- **增**：追加一个 `{"code": "6位基金代码", "name": "基金名称"}` 项

- **删**：删除对应项

- 修改后推送到 `main`，下一个美股交易日 CI 自动纳入/剔除

- 文件缺失或格式错误时回退内置默认清单（向后兼容，不报错）

- 也可用本地图形面板 `github-data-maintainer.html`（GitHub 数据维护面板）编辑本文件



## 本地运行



```bash

pip install -r requirements.txt

python scripts/run_daily.py --out output        # 正常（交易日判断）

python scripts/run_daily.py --force --out output  # 强制运行（测试）

python scripts/render_html.py --json output/daily_report.json --history output/predictions.jsonl --out docs/index.html  # 渲染网页

```



## 静态网页



GitHub Pages 发布（main 分支 /docs 目录），地址：`https://homjanon.github.io/qdii-nav-tracker/`



页面区块：⭐今晚净值预测（基金卡片数随 `config/funds.json` 清单，待公布显示预测、已公布显示预测vs实际对照）→ 历史预测验证（命中率/MAE/对照表）→ 美股含量总览（Chart.js 条形图）→ 持仓质量表 → 疑似调仓（全部十大持仓+中文名）



## 输出示例（summary.md）



| 代码 | 基金 | 披露美股% | NDXβ | 静态方向% | 静态MAE | 滚动MAE |

|------|------|:---:|:---:|:---:|:---:|:---:|

| 021842 | 国富全球科技C | 49.8% | 1.83 | 95.5% | 1.08 | 0.69 |



## 支持市场与数据源



| 数据 | 主源 | 备源/兜底 |

|---|---|---|

| 十大持仓 / 年报全持仓 | 天天基金 F10（HTTP/1.1 直连） | 本地缓存 holdings_cache.json（F10 超时兜底，90 天有效） |

| 基金净值 | 东财 f10/lsjz 直连 | akshare（东财） |

| 美股日线 | **yfinance（Yahoo，含当天实时，2026-08-19 首选）** | akshare 新浪源（历史兜底）→ 腾讯快照 |
| 港股 / A股日线 | akshare（新浪源） | 腾讯 qt.gtimg.cn 实时快照（当日预测兜底） |

| 日股 / 韩股 | 东财 push2his（JP=176/KR=177 secid） | yfinance（.T/.KS）→ 腾讯快照（kr/jp 前缀，当日） |

| USD/CNH 汇率 | 中行牌价 currency_boc_safe | 东财 push2his（curl_cffi）→ yfinance |

| NDX / INX / HSI 指数 | akshare（新浪） | yfinance（兜底） |

| 美股交易日历 | pandas-market-calendars（NYSE） | weekday 近似 |



> 多源降级链参考 portfolio（净值/汇率）、douban-tracker（腾讯行情）、cmb-tracker（fallback_chain）生产验证经验。

> ⚠️ **美股日线必须用 yfinance（2026-08-19 修复）**：新浪美股日线 `ak.stock_us_daily` 滞后一天（收盘后次晨才更新），曾导致 8/18 美股大跌日预测仍用 8/17 大涨数据、10 个基金 9 个方向错误。yfinance `period="1y"` 含当天实时价（美东收盘后即更新），GitHub Actions 北京 06:00 运行时美股已收盘，预测方向与实际一致。



## 已覆盖的日韩股持仓



| 代码 | 名称 | 市场 | 东财 secid |

|---|---|---|---|

| 285A | KIOXIA 铠侠 | JP | 176.285A |

| 6857 | 爱德万测试 | JP | 176.6857 |

| 005930 | 三星电子 | KR | 177.005930 |

| 000660 | SK海力士 | KR | 177.000660 |



## 已知边界



- 十大持仓仅覆盖基金仓位 40~65%，剩余仓位影响会分摊进 NNLS 权重（看趋势、不直接当精确占比）

- 披露为季末时点（6/30），7/1 后调仓会降低静态 R²；半年报披露后可升级全持仓口径

- 幅度预测（MAE ~0.5-1.1pp）只能参考，方向预测（77~95%）更可靠；方向偏差源于"披露持仓≠实际持仓"，NNLS 能缩小但无法完全消除

- 腾讯快照兜底在"日韩已收盘+美股未收盘"窗口（北京 9:00-16:00）手动触发时可能跳过日韩股（已知边界，暂不处理）

