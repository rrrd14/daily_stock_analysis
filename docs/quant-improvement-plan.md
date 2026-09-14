# 简易量化改进计划：基于当前实现的修订版

修订日期：2026-09-14（北京时间）。审查基线：HEAD `54ada83` + 当时的 E2E/测试修复工作区。状态：下一阶段计划，未宣称下列待办已经实现。

详细设计：[数据与时间契约](architecture/market-data-time-contract.md)。已有能力：[第一阶段运行证据](architecture/quant-verifiability-phase1.md)。本次独立 review 保存在本地 `.claude/reviews/current-review/REVIEW.md`。

Docker 恢复后的补充证据见[环境与测试记录](testing-environment-and-results.md)：Web、空回测证据重启保留和核心三时区检查通过；不同 UID 的目录写入失败已复现。在线 Yahoo 历史两标的均返回 750 条，588000 实时报价超过 40 秒，601398 返回上周五行情并标记 stale。上述结果补充验收事实，不代表下列修复已完成。

## 1. 产品目标与不变的边界

保留 Web 对话作为入口，让用户可以查数据、解释指标、提出研究假设，但事实、指标、策略规则、模拟成交和收益统计均由程序产生，并能够追溯到输入快照。

首个量化版本限定：固定标的池、日频、多头、离线研究和模拟账户。不接实盘、不做杠杆/分钟级交易、不做无限参数搜索。具体策略用于验证工程链路，不作为投资收益承诺。

对“每天能取得真实数据”的工程定义是：每次计划执行都产生状态；能拿到时记录来源、源时间与覆盖；拿不到时明确失败或受限降级；不会把旧数据、估算数据或模型生成内容伪装成当日事实。任何一次在线 PASS 都不能替代长期运行证据。

## 2. 当前完成度

| 能力 | 当前状态 | 下一步 |
|---|---|---|
| 北京时间工具、定时器、模型时钟 | 已有主干，未覆盖所有服务 | 处理任务/新闻/持仓/API 的剩余宿主机时钟 |
| ETF 长历史自动/指定切源 | 已实现 | 修复短窗口截止日期、能力和超时分类 |
| 实时报价 provenance | adapter/Agent/Pipeline 部分贯通 | API/schema/Web 全链路透传，保留字段来源 |
| 指标 None 与 NaN 防御 | 已改善，评分仍存在严重缺口 | 有效性先于计算/打分；保留风险 |
| 不可变运行证据 | 报告评估阶段已实现 | 增加数据质量等级、快照身份与重放验证 |
| Docker E2E | 脚本与 CI 配置已新增 | Linux UID 权限、同一镜像验证、构建上下文 |
| 连续账户回测 | 未实现（已落地回报率引擎 `daily_return`，仅算日频回报率） | 账户/持仓/成本/费用建模 |
| 自然语言创建策略实验 | 未实现 | 后续使用结构化实验参数，不执行模型任意代码 |
| 每日模拟跟踪与可用性历史 | 未形成量化闭环 | 复用调度器，记录运行状态和幂等结果 |

## 3. 第一优先级：阶段 1.1，补齐数据与时间可信度

以下工作包按顺序推进。可以作为独立 PR/检查点，但不要求现在创建远端 PR。

### WP1：指标有效性与评分保护（优先处理 R1、R5）

状态：**已实现**（2026-09-14）。证据：`src/stock_analyzer.py` 新增 `IndicatorValidity` / `SignalStatus` / `REQUIRED_INDICATORS`，`TrendAnalysisResult` 追加 `signal_status` / `actionable` / `score_status` / `indicator_quality`；`data_provider/base.py` 量比补充非有限值防御；`src/agent/tools/analysis_tools.py` 仅在展示层舍入且保证 JSON 有限。回归见 `tests/test_stock_analyzer_indicator_validity.py`（11 项）。复现脚本 `.claude/reviews/current-review/reproduce.py` 的 `missing_still_scored` 由「77 分/强烈买入/空风险」变为「54 分/持有/不足并列出缺口」，`partial_volume_window` 与 `price_gap` 两个入口均判定为不足/缺口。以下为原始交付清单。

改动边界：`src/stock_analyzer.py`、`data_provider/base.py`、`src/agent/tools/analysis_tools.py`、受影响的报告/Schema/前端展示。

交付内容：

- 用 explicit validity 区分 valid、insufficient、invalid，而不是从默认枚举猜测有效性。
- MA 窗口、前五日成交量、RSI 涨跌窗口、MACD 预热和缺口规则统一。
- 缺失指标不参与正常状态评分；保留上游风险；输出 signal_status。
- 指标数据不完整时允许展示已有效的指标，不生成可执行买卖信号。
- 只在展示层舍入；所有 JSON 数值有限，不输出 NaN/Infinity。

验收：20 行+缺量样本不再生成“77分强烈买入”；前五日少一条量时两个入口都返回不足；价格窗口缺口不会被静默视为零涨跌；完整行情的数学结果保持一致。新增回归必须覆盖实际缺陷，而非仅检查字段存在。

### WP2：统一日期边界与北京时间契约（R2、R4）

状态：**已实现**（2026-09-14）。R2：`src/services/history_loader.py` 所有路径（短/长窗口、自动/显式来源）共用同一 resolved 起止日期，并强制裁剪，显式 `target_date` 下不再返回未来行情，新增回归 `tests/test_history_loader.py::test_target_date_is_enforced_on_network_fallback`。R4：任务队列/任务服务、报告渲染、历史报告、持仓 `as_of` 与更新时间、新闻发布时间归一化与时效窗口、通知与邮件/推送、API 时间戳、大盘复盘文件名、Bot 状态时间共 14 个文件改用 `src/time_utils`；有意保留的宿主/外部时钟：`src/core/trading_calendar.py` 的 market-tz 与未知市场 fail-open、`bot/platforms/*` 平台签名时间戳、`api/v1/endpoints/usage.py`（原已用北京时区）、`src/services/stock_service.py` 的 `update_time`（随 WP3 报价契约一并处理）。以下为原始交付清单。

改动边界：历史加载器、time_utils、交易日历、task/report/history/portfolio/search 服务、对应 API 和 Web 时间格式化。

交付内容：

- 短长窗口、缓存/网络、自动/指定来源均使用同一 resolved date range。
- 显式 target_date 优先；否则沿用冻结目标日；没有历史目标才依据市场和程序时间解析。
- 禁止返回目标日之后的数据；缓存按真实唯一交易日和最近已完成 session 判断，不在周末无谓要求“周日日线”。
- 枚举并修复业务时钟，不替换用于耗时计算的 monotonic，也不修改交易所日期的含义。
- 新时间戳带 +08:00；旧 SQLite naive 时间在单一适配边界处理，未明来源历史数据不自动换算。

验收：同一 UTC 时刻在 UTC/上海/洛杉矶宿主环境下，API、模型当前日期、任务时间与北京时间展示一致；2024 截止请求不能返回2026数据；跨北京时间午夜、周末、节假日、美国夏令时转换均有测试。

### WP3：报价证据贯通与来源选择（R3）

状态：**R3 已实现**（2026-09-14）；**来源失败分类已实现**（`data_provider/base.py` 新增 `FETCH_ERROR_CATEGORIES` + `classify_fetch_error`，`get_daily_data` 的 attempts 增加 `reason`，实时行情失败日志带类别，测试见 `tests/test_fetch_error_classification.py`）；**显式来源不静默 fallback 已由既有「按 source 过滤 fetchers」逻辑保证，并新增回归锁定**。仍未实现：**按能力声明并选择数据源**（目前只有按市场路由 + `_DAILY_MARKET_FETCHER_SUPPORT` 过滤，没有端点能力矩阵与请求级能力协商）；**成交量单位归一**只做到「透传 `volume_unit` + Pipeline 拒绝合并未知/不一致单位」，港股 EM 实时路径仍为 `unknown`（不据文档猜测单位）。`src/services/stock_service.py` 透传全部证据字段并把 `update_time` 明确为抓取时间；`api/v1/schemas/stocks.py` 追加 11 个可空字段（旧客户端可忽略）并由 `api/v1/endpoints/stocks.py` 转发；新增 `tests/test_stock_quote_evidence.py`（8 项）。复现探针 `web_quote_contract` 由「只有宿主 `update_time`」变为「保留 `quote_time=2024-01-01T15:00:00+08:00`、`freshness=stale`、`is_realtime=false`、`source`、`volume_unit`、`field_sources`、`served_at`、`session_date`」。Web 侧当前没有消费该接口的页面，未做前端改动。以下为原始交付清单。

改动边界：`data_provider/realtime_types.py`、各适配器、`stock_service.py`、`api/v1/schemas/stocks.py`、Web 行情和 Agent 工具。

交付内容：

- 区分 quote_time、fetched_at、served_at、市场 session_date。
- API 与客户端追加 source、unit、freshness、quality/field_sources；旧字段保持兼容并标明含义。
- 无时间来源为 unknown；过期报价可供查看但不允许当成当前盘中数据合成指标。
- 来源失败分类为超时、网络、鉴权、权限/额度、空数据、不支持和无效载荷；不把超时等同无历史。
- 按能力选择数据源；显式来源模式不悄悄 fallback；自动模式返回所有尝试的脱敏摘要。

验收：把2024报价传进API，Web仍显示其旧行情时间和 stale；第二来源补充的PE不能继承第一来源的价格时间；股票/ETF/港美股不支持字段明确缺失。

### WP4：日线快照与数据质量门槛（R7）

状态：**第一阶段 + 运行引用已实现**（2026-09-14）：新增不可变快照表 `market_data_snapshots` 与仓储 `src/repositories/market_snapshot_repo.py`（只增不改、身份 = 口径 + 区间 + 内容哈希、稳定序列化哈希），质量门槛 `assess_data_quality`（`verified`/`partial`/`unknown`）与策略前置校验 `ensure_input_eligible`，覆盖判定 `compute_coverage`（只依据真实数据首尾，不补齐）。`backtest_runs` 追加 `snapshot_id`/`data_quality_status`/`input_eligibility`/`engine_kind`/`engine_version`；`run_backtest()` 支持可选 `snapshot_id` 与 `engine_kind`：报告评估保持原语义并如实记录快照质量，**其它策略引擎必须引用合格快照，否则入口拒绝**。只读查询已补齐：`GET /api/v1/backtest/snapshots` 与 `/{snapshot_id}`（`include_bars` 可选）+ Agent 工具 `get_market_snapshot`；单份 payload 超 5 MiB 拒绝写入，`storage_stats()` 提供体积概览。回归 `tests/test_market_snapshot_freeze.py`（14）+ `tests/test_backtest_snapshot_link.py`（7）+ `tests/test_market_snapshot_api.py`（8），容器探针 28 项三时区全 PASS。**已补齐**：前端回测运行卡片展示快照引用（质量等级、输入资格、引擎类型/版本 + 按需读取快照元数据核对来源/复权/币种/单位/冻结区间，含 5 项 Web 回归）；按交易日历推断预期 session 数（`trading_calendar.count_sessions()` + `compute_coverage(expected_sessions=...)`，日历不可用时明示 `coverage.basis=endpoints_only` 而非伪装完整）；快照导出工具 `scripts/export_market_snapshots.py`（只读、逐份哈希核对、退出码语义）。**2026-09-14 复核后修订（R1–R5）**：① 冻结前逐行校验 OHLCV/有限值/日期唯一与价格关系，坏行与重复行不计入覆盖（覆盖改用**有效唯一交易日集合**）；② `required_rows` 与 `SNAPSHOT_QC_VERSION` 进快照身份，提高数据要求会得到新快照并经重新评估；③ 未实现的 `engine_kind` 一律拒绝（不再复用旧报告引擎），证据中 `snapshot.consumed=false`；④ `docker_e2e.sh` 提交可执行位并有回归锁定；⑤ 导出 CLI 可独立运行并强制 UTF-8 输出。**仍未实现**：按「上市信息」收紧预期区间（仓库当前无运行时上市日期源，仍以首尾判定兜底，故不会因缺上市数据放宽）。**已实现（第二阶段）**：回报率引擎 `daily_return`——按引擎注册表分派、从冻结 bars 计算日频简单收益率、总回报率与年化回报率（几何、252 交易日/年）并校验标的/区间、记录 `consumed=true`（见 `tests/test_daily_return_engine.py`）；不建模资金/费用/滑点/持仓，也不做组合账户回测。以下为原始交付清单。

改动边界：优先复用历史加载器和 backtest_runs；新增小型快照元数据/仓储，不平行重写行情采集框架。

交付内容：

- 快照保存标的、市场、原始交易日、价格复权口径、货币、成交量单位、来源/实际端点、抓取时刻、内容哈希。
- 区分 requested_days 与明确日期区间；日期区间覆盖基于交易日历和上市信息，缺失不可用0或前值补齐。
- 不在未确认单位/复权时合并来源；明确哪些数据可研究、哪些只能展示。
- 已完成运行引用冻结快照；可变 StockDaily 缓存不承担审计身份。

验收：同源不同复权得到不同快照身份；重拉后旧运行可重放；缺失期间不能被标成完整三年；unknown 快照不能用于默认策略收益计算。

### WP5：部署与日常可用性验证（R6）

改动边界：现有 scheduler、`scripts/`、Docker/CI、诊断服务和文档。

交付内容：

- 修正 E2E 的 UID/GID 权限；验证同一镜像的构建、运行和发布摘要。
- 不依赖外部服务的 Docker 门禁与网络健康探测分开；先确认服务可启动，再确认数据可用。
- 每个数据任务保存 run_id、planned_at、started_at、finished_at、预期交易日、各来源尝试、结果覆盖和质量。
- 日常任务采用幂等键，防止启动立即执行与定时执行重复落库；重启后可查失败/中断。
- 验证交易日盘前/盘中/盘后与非交易日场景，复用现有调度器，不创建第二套常驻调度。

验收：Linux 非UID1000宿主也能通过权限检查；同一幂等键重复执行不重复写；盘后缺最新交易日日线会标记不完整；周末最近收盘是 last_closed_session 而不是伪造实时；失败不触发虚假“更新成功”。

## 4. 阶段 1.1 的退出条件

所有条件同时满足才能开始默认可信策略回测：

1. R1–R5 的复现已变成回归测试并通过。
2. 数据源元信息、API 字段、Web/Agent 展示契约一致。
3. 两个代表标的在相同明确日期区间生成合格冻结快照；不以750条近似代替三年区间。
4. 北京时间与市场交易日测试矩阵通过。
5. Docker E2E 在目标 Linux 环境通过；目标服务器镜像 digest 与被测镜像一致。
6. 至少覆盖一个完整交易日的盘前/盘中/盘后观测和一个非交易日验证。建议积累5个交易日数据作为初始可靠性样本，但不把这个样本宣传为永久可用性保证。

真实网络不可用时可继续做确定性开发和评审，但不能签署“目标服务器每天行情已验证”的验收。

## 5. 第二阶段：日频回测（先算回报率，账户/组合后续）

入口复用 Web 回测页、API 服务层和运行记录。已落地 `engine_kind=daily_return`（回报率引擎：从冻结快照计算日频简单收益率与总回报率，不建模资金/费用/持仓，见 `tests/test_daily_return_engine.py`）；完整的连续账户日频回测（含资金/费用/滑点/持仓）仍待实现，届时再新增 engine_kind，保留 `ai_report_evaluation` 的原语义，不把多个结果混在同一汇总里。

最小实验规格：固定标的池、固定起止日期、初始资金、策略ID/版本、参数、冻结快照ID、费用模型版本、滑点模型版本、市场规则版本、基准与再平衡频率。首个策略选择可以验证交易链路的简单确定性规则；选定后必须固化，避免根据已看到的收益反复改口径。

引擎职责：按时间排序生成信号；信号只读取当时可知信息；在下一可成交时点应用成交规则；更新订单、成交、持仓、现金和权益；输出逐日曲线和逐笔账本。停牌、最小交易单位、结算约束、涨跌停和费用按经确认的市场规则版本处理，不在本计划中硬编码费率。

复用 portfolio service 中可复用的现金/费用/持仓核算，但先区分“用户真实记账”和“回测模拟账本”，不可往用户账户写模拟成交。

验收：手算小样本对账；不出现负现金/无持仓卖出；净值与现金+持仓估值一致；无未来函数；同快照与参数重复结果一致；扣除成本与基准比较明确。后续再加入样本外区间，参数选择区间与评估区间分开。

## 6. 第三、四阶段

第三阶段是自然语言实验：LLM 将需求转成有限 schema，程序校验并展示可编辑的实验规格；按规格调用同一引擎；模型不能生成任意代码执行，也不能自行填收益。返回结果引用真实 run_id 与输入快照，AI 解读与程序指标分开展示。

第四阶段是每日模拟跟踪：调度器更新合格日线快照→规则信号→模拟订单→账本→日报；记录应执行而未执行的任务；区别“无交易信号”“行情不可用”“模型解释失败”。解释失败不改变确定性的账户结果。

## 7. 实施顺序与工作量

建议顺序：WP1 → WP2 → WP3 → WP4 → WP5 → 连续账户回测。前两包最高优先；服务器观测等待期间可以进行不依赖真实网络的后续开发。

工作量估计仅用于排期：WP1–WP3 各约1–3个开发日，WP4–WP5 各约2–4个开发日；第二阶段首个可对账引擎约5–10个开发日。实际受现有客户端联动、旧数据质量和目标服务器网络影响，不是完成承诺。

每包交付应包含：代码、字段/行为说明、缺陷回归、未验证项、镜像/运行编号以及回滚方式。先合格一个最小闭环，再扩标的和功能。

## 8. 回滚与协作

不删除现有运行证据和用户数据库。新字段优先追加；必要状态变化保留兼容层。数据库变更先备份、预览影响，保留旧镜像 digest；不要将旧时区不明记录批量改写为北京时间。

本计划和设计作为后续协作入口，CHANGELOG 记录已实现行为；CHANGES_SUMMARY 保留本轮提交的历史快照。未来每次完成一个工作包更新完成度和验收证据，不把“计划”写成“已修复”。
