# 本次改动汇总（Commits + Working Tree）

> ## 本分支交付状态（2026-09-14，本地分支，按你的要求**未开 PR**）
>
> - 分支：`feat/quant-verifiability-hardening`（已推送 origin；远端 `main` 未改动），本地共有 **14 个**提交（早先 6 个 + 上一批 7 个 + 本轮 R1–R5 修复 1 个），工作区干净。
> - 提交（英文 message、无 `Co-Authored-By`）：
>   1. `388d9ea` `fix: gate indicator validity, date bounds and quote evidence` — WP1+WP2+WP3（35 文件，+1349/−178）
>   2. `be0b55a` `feat: add immutable market-data snapshots with quality gating and export` — WP4（14 文件，+1846）
>   3. `3e7b5ad` `feat(web): surface frozen snapshot evidence in the backtest card` — 前端（+286/−8）
>   4. `edde47c` `chore: single-source agent skills and shrink the docker build context` — 治理 + Docker 上下文
>   5. `6b24af1` `ci: add docker e2e job and run frontend unit tests in web-gate` — CI
>   6. `24ff6eb` `docs: record quant verifiability work, contracts and environment results` — 文档
>   7. `43e41f2` `docs: record the final local verification results and delivery state` — 交付状态与实测记录
>   8. `521b6c5` `fix: validate frozen payloads, gate engines and CI script exec bits` — 复核 R1–R5 修复（详见下方专段）
>
> ### 本地验证结果（均为本机实跑）
>
> | 项 | 命令 | 结果 |
> | --- | --- | --- |
> | flake8（CI 同口径，整仓） | `python -m flake8 . --count --select=E9,F63,F7,F82` | 0 问题，rc=0 |
> | 快照 / 交易日历 / 导出 / 回测引用 | `pytest -q`（6 个文件） | **67 passed** |
> | 受影响面（21 个文件，`-m "not network"`） | `pytest -q` | **274 passed** |
> | 前端（Linux `node:20` 容器，与 CI `web-gate` 同环境） | `npm ci && npm run lint && npm run build && npm run test` | 全 rc=0；vitest **47 文件 / 405 passed / 2 skipped** |
> | Docker E2E（真实镜像 + 非 root 运行） | `bash scripts/docker_e2e.sh` | **14 项断言全 PASS**：镜像内 `tsc -b`+vite 构建成功、`/api/health` 等只读接口全 200、首页引用的 `/assets/*` 存在、以 `dsa` 运行、`data/logs/reports` 在 UID/GID 1000 下可写、容器保持运行 |
> | AI 资产治理 | `python scripts/sync_agent_skills.py --check` | OK（4 files in sync） |
>
> ### 未验证项（诚实声明）
>
> - **未在本地重跑完整离线套件**：本机终端会话本次极不稳定（命令时通时断、独立进程卡在 CPU=0），最近一次完整绿是本次改动**之前**的 1834 passed。**范围澄清**：本分支全量 diff 是 71 文件（含数据提供者、指标、任务、API 等），而**本轮 R1–R5 修订**只触及快照仓储、交易日历、回测服务、导出脚本、CI 权限与前端文案；这两者的验证记录已分别标明，勿相互代表。
> - `scripts/check_ai_assets.py` 在 Windows 上会因 `CLAUDE.md` 软链被检出为普通文件而报错（git index 中为 `120000`，Linux 上是真软链，CI 可过）。
> - 前端未做真实浏览器 + 后端联调；`count_sessions` 用 fake calendar 做确定性测试，**未**断言真实交易日历数据的准确性。
> - CI 新增的两项（`docker-e2e` job、`web-gate` 加跑 `npm run test`）尚未在 GitHub Linux runner 上执行过；本次 e2e 属于 Windows/Docker Desktop 上的等价验证，R6 在真实 Linux runner（调用者 UID≠1000）下仍待证明。
>
> ## 独立复核（2026-09-14）与 R1–R5 修复
>
> 复核范围 `54ada83..43e41f2`（71 文件）：确认 WP1 评分保护、WP2 日期边界、WP3 报价证据确有改善，但指出 5 个**已复现**缺陷。复核原始结论见 `.claude/reviews/latest-review/REVIEW.md`；修复与验证如下。
>
> | # | 复核问题 | 修复 |
> | --- | --- | --- |
> | R1 P1 | `scripts/docker_e2e.sh` 的 Git mode 是 `100644`，CI 用 `./scripts/docker_e2e.sh`，Linux 下退出 **126** | 提交可执行位（`100755`）；新增 `tests/test_ci_script_modes.py`，校验 workflow 中每个 `./path/script` 调用在 Git 索引里都是 `100755`（正则失效时会显式失败而非静默通过） |
> | R2 P1 | NaN、无价格、重复日期的行情仍获 `verified` 与策略资格 | 新增 `validate_bars()`：缺 OHLCV、NaN/Infinity、日期非法/重复、价格关系不成立 → `unknown` 且 `input_eligibility=false`；覆盖判定改用**有效唯一交易日集合**（`quality.coverage.counted_sessions`），坏行与重复行无法凑满覆盖 |
> | R3 P1 | 接受 `portfolio_daily` 标签却仍调用旧报告引擎，未消费冻结行情 | 未实现的 `engine_kind` 在入口一律拒绝；报告评估仍可附加快照作为**引用**，但证据里写明 `snapshot.consumed=false` 并在 `limitations` 说明「引用不等于消费」 |
> | R4 P2 | 数据要求从 5 条提高到 100 条仍复用旧的合格状态 | `required_rows` 与 `SNAPSHOT_QC_VERSION` 一并进快照身份：提高要求会得到**新快照**并重新评估，两种创建顺序都有回归 |
> | R5 P2 | `python scripts/export_market_snapshots.py --help` 报 `ModuleNotFoundError: No module named 'src'` | 按仓库脚本惯例引导仓库根目录；顺带修复在 Windows 下中文提示触发 `UnicodeEncodeError` 导致导出整体失败（这两个缺陷都由新增的子进程 CLI 测试抓出） |
>
> 本轮修复的验证（本机实跑，详见 `docs/testing-environment-and-results.md` §11）：受影响集 **92 passed**（8.49s）、更广回归面（23 文件，`-m "not network"`）**292 passed**（10.39s）、flake8（CI 同口径）**0 问题**、独立复现探针（复刻复核场景）**13 passed / 0 failed**、Linux 容器探针确认 tar 内文件为 `-rwxr-xr-x` 且 `bash -n` 通过（blob 无 CR，LF）。修复提交：`521b6c5`。
>
> 本轮**未改前端**：Web 快照卡片沿用上一轮已验证结果（47 文件 / 405 passed / 2 skipped），改动集中在后端契约与脚本。


> 下面是各工作包的原始完成说明，保留作为历史记录。
>

> **WP1 已完成（2026-09-14）**：技术分析指标有效性与评分保护（R1/R5）已实现并通过回归。改动：`src/stock_analyzer.py`（新增 `IndicatorValidity`/`SignalStatus`/`REQUIRED_INDICATORS` 与 `signal_status`/`actionable`/`score_status`/`indicator_quality`，评分按有效性门控，风险因素不再被覆盖）、`data_provider/base.py`（量比非有限值防御）、`src/agent/tools/analysis_tools.py`（展示层舍入 + JSON 有限）、新增 `tests/test_stock_analyzer_indicator_validity.py`（11 项）。复现脚本结果：`missing_still_scored` 77 分/强烈买入/空风险 → 54 分/持有/列明缺口；`partial_volume_window`、`price_gap` 两个入口一致判为不足/缺口。下一步 WP2（R2/R4：日期边界与剩余宿主机时钟）。

> **WP2 已完成（2026-09-14）**：日期边界与北京时间业务时钟（R2/R4）已实现并通过回归。R2：`src/services/history_loader.py` 所有路径共用同一 resolved 起止日期并强制裁剪，显式 `target_date` 下不再返回未来行情；新增 `tests/test_history_loader.py::test_target_date_is_enforced_on_network_fallback`。R4：14 个文件改用 `src/time_utils`（task_queue/task_service/report_renderer/history_service/portfolio_service/portfolio_risk_service/portfolio_repo/notification/search_service/market_review/api app+health+analysis/bot status）。有意保留：交易所 session 计算、`monotonic` 耗时、`bot/platforms/*` 平台签名时间戳、`usage.py`（原已北京时区）、`stock_service.update_time`（归入 WP3）。下一步 WP3（R3：报价证据贯通与来源选择）。

> **WP3 已完成（R3 + 失败分类，2026-09-14）**：实时行情证据贯通。`src/services/stock_service.py` 透传 `quote_time`/`fetched_at`/`served_at`/`session_date`/`source`/`freshness`/`age_seconds`/`volume_unit`/`field_sources`/`is_realtime`/`freshness_note`，`update_time` 明确为抓取时间（北京 +08:00）；`api/v1/schemas/stocks.py` 追加 11 个可空字段，`api/v1/endpoints/stocks.py` 转发；新增 `tests/test_stock_quote_evidence.py`（8 项）。复现探针由「只有宿主 `update_time`」变为「保留源时间 + stale + is_realtime=false + field_sources」。Web 侧无该接口消费页面，未改前端。**来源失败分类**：`data_provider/base.py` 新增 `FETCH_ERROR_CATEGORIES`/`classify_fetch_error`（8 类），`get_daily_data` attempts 增加 `reason`，实时失败日志带类别；超时/鉴权不再与「确实无历史」混淆；显式来源不静默 fallback 加回归锁定；新增 `tests/test_fetch_error_classification.py`（7 项）。容器探针扩到 18 项，三时区全 PASS。**仍未实现**：按能力声明/选择数据源（只有市场路由）、成交量单位归一（仅透传 + 拒绝合并未知单位）。
> **WP4 已完成（第一阶段 + 运行引用，2026-09-14）**：不可变行情冻结快照（R7）。表 `market_data_snapshots` + `src/repositories/market_snapshot_repo.py`：身份 = 口径（来源/复权/币种/单位）+ 区间 + 内容哈希 → 同源不同复权得到不同 `snapshot_id`；只增不改、幂等、不覆盖旧快照（可重放）。质量门槛 `assess_data_quality`（`verified`/`partial`/`unknown`）+ 策略前置 `ensure_input_eligible()`；`compute_coverage` 只据真实数据首尾判定，不用 0/前值补齐。`backtest_runs` 追加 `snapshot_id`/`data_quality_status`/`input_eligibility`/`engine_kind`/`engine_version`，`run_backtest()` 支持可选 `snapshot_id`+`engine_kind`：报告评估保持原语义并如实记录，策略引擎必须引用合格快照否则入口拒绝。只读查询已补齐：`GET /api/v1/backtest/snapshots` + `/{snapshot_id}`（`include_bars` 可选）+ Agent 工具 `get_market_snapshot`；单份 payload 超 5 MiB 拒绝写入，`storage_stats()` 提供体积概览。回归 29 项（14+7+8），容器探针扩到 28 项、三时区全 PASS。**未实现**：按上市信息收紧预期区间（无运行时上市日期源）、真正的 `portfolio_daily` 引擎（第二阶段）。

> **治理修复（2026-09-14）**：① `.agents/` 明确为本地脚手架并加入 `.gitignore`，`.agents/skills/` 改由新增 `scripts/sync_agent_skills.py` 从单一真源 `.claude/skills/` 生成；`check_ai_assets.py` 新增「拒绝入库 `.agents`」与「镜像漂移校验」；AGENTS.md 与 `.claude/skills/README.md` 同步更新。② `tests/litellm_stub.py` 的 `_DummyRouter` 接受真实 `Router` 的构造参数，消除「全量通过、子集失败」的顺序依赖（原先 3 组子集组合共 11+2 个假失败，现 63/70/86 全通过）。③ `.dockerignore` 增加前端 `node_modules`/`dist`、`.git/`、`.github/`、`.agents/`、`.claude/`、`tests/` 与 pytest 缓存，构建上下文由 ~2.00 MB 降至 ~1.02 kB（docker build 仍 RC=0）。
>



> **后续复核（2026-09-14，北京时间）**：下文保留原提交/工作区历史说明。独立复核已重新跑通后端离线测试（1778 passed）与 Web 测试（398 passed，2 skipped），同时发现缺失指标仍参与评分、短历史截止日期、Web 时效字段和剩余宿主机时间等未覆盖问题。下一步以 [修订计划](docs/quant-improvement-plan.md) 和 [详细设计](docs/architecture/market-data-time-contract.md) 为准；本地完整 review 见 `.claude/reviews/current-review/REVIEW.md`。原 Docker PASS 属于此前环境记录，本次未重跑 Docker。

> 用途：快速了解本次任务的全部改动（**均已提交到 `feat/quant-verifiability-hardening`**）、实际验证结果与剩余缺口。
> 生成时间：2026-09-13
> 基线 commit：`f76e8c77cb82651136cbaa2bbd33ffdca05ccbc2`（`origin/main`）
> 当前 HEAD：`24ff6eb`（分支 `feat/quant-verifiability-hardening`，已推送 origin；远端 `main` 未改动）

---

## 0. 一句话概览

本次任务现已全部提交到分支 `feat/quant-verifiability-hardening`，工作区干净，共 **12 个提交**（两批）：

1. **早先的 6 个 commit**（`7db78d5`…`54ada83`，见 0.1）：北京时间统一、实时行情时效元信息、技术指标 null 语义、回测证据链、历史行情来源诊断、文档与 Docker 缓存目录；此前记录的「未提交改动」（`scripts/docker_e2e.sh` 接入 CI、`is_us_stock_code` 空值防护、Windows 本地测试隔离）已一并提交。
2. **本次的 6 个 commit**（`388d9ea`…`24ff6eb`）：WP1/WP2/WP3 可信度修复、WP4 冻结快照与只读导出、前端快照证据、AI 资产治理与 Docker 上下文、CI、文档。

---

## 0.1 早先的 6 个提交（`f76e8c7..54ada83`）

| # | Commit | 类型 | 标题 |
|---|--------|------|------|
| 1 | `7db78d5` | fix | use Beijing time as the application clock regardless of host timezone |
| 2 | `1b4e1ac` | fix | expose realtime quote provenance and stop mislabeling stale data as live |
| 3 | `c7453b7` | fix | return null instead of zero for unavailable technical indicators |
| 4 | `7fc3458` | feat | add verifiable backtest run evidence chain (phase 1) |
| 5 | `81adf10` | feat | allow explicit history source and expose coverage diagnostics |
| 6 | `54ada83` | docs | document Beijing clock, data provenance and backtest evidence chain |

### Commit 1 — `7db78d5` 北京时间统一

> fix: use Beijing time as the application clock regardless of host timezone

新增 `src/time_utils.py`（`BEIJING` / `beijing_now` / `beijing_today` / `beijing_now_naive` / `clock_context`），并将 `datetime.now()` / `date.today()` 调用点统一改走该模块：storage ORM 默认值、scheduler（`schedule.at` 使用 `Asia/Shanghai`，新增 `pytz` 依赖）、日志文件名与时间戳转换器、main 入口、market analyzer、agent 会话。

**涉及文件**：`.env.example`、`main.py`、`requirements.txt`、`src/agent/conversation.py`、`src/logging_config.py`、`src/market_analyzer.py`、`src/scheduler.py`、`src/storage.py`、`src/time_utils.py`（9 files, +97/-45）

### Commit 2 — `1b4e1ac` 实时行情时效元信息

> fix: expose realtime quote provenance and stop mislabeling stale data as live

`realtime_types` 新增 `volume_unit` / `quote_time` / `fetched_at` / `field_sources` 与 `provenance()` 时效信息。Pipeline 在合成实时 K 线前校验时效与 `quote_date`，缺失 `pct_chg` 保持 NaN，仅在成交量单位已知且一致时才合并成交量。Akshare 成交量归一为股；yfinance 不再用估算成交额替代换手；polygon 接受 DELAYED 状态。向 agent 与 analyzer prompt 注入 `[PROGRAM_CLOCK]`，避免模型从旧上下文推断当前日期。

**涉及文件**：`data_provider/akshare_fetcher.py`、`data_provider/polygon_fetcher.py`、`data_provider/realtime_types.py`、`data_provider/yfinance_fetcher.py`、`src/agent/runner.py`、`src/analyzer.py`、`src/core/pipeline.py`、`tests/test_analyzer_news_prompt.py`、`tests/test_pipeline_augment_realtime.py`、`tests/test_polygon_fetcher_status.py`（10 files, +148/-41）

### Commit 3 — `c7453b7` 技术指标 null 语义

> fix: return null instead of zero for unavailable technical indicators

MA60/MACD/RSI/量比在数据不足时返回 `None` 并在 `risk_factors` 记录缺口，不再静默报 0。RSI 仅在真实横盘时给出中性 50。指标窗口使用完整周期不做提前取整，量比按数据源隔离。Agent 分析工具轮次对 `None` 安全。

**涉及文件**：`data_provider/base.py`、`src/agent/tools/analysis_tools.py`、`src/stock_analyzer.py`（3 files, +165/-55）

### Commit 4 — `7fc3458` 回测证据链（phase 1）

> feat: add verifiable backtest run evidence chain (phase 1)

将每次 CLI/API 评估持久化为不可变 `backtest_runs` 记录（`run_id`、参数、代码 SHA256、逐项输入/输出与显式限制），状态为 `completed/partial/failed/empty`。新增 `GET /backtest/runs` 与 `/backtest/runs/{run_id}`、只读 agent 工具、Web 运行卡片与原始 JSON 下载。技能归属改用 `analysis_skill_ids` 而非文本匹配。

**涉及文件**：`api/v1/endpoints/backtest.py`、`api/v1/schemas/backtest.py`、`apps/dsa-web/src/api/backtest.ts`、`apps/dsa-web/src/components/BacktestRunCard.tsx`(+test)、`apps/dsa-web/src/pages/BacktestPage.tsx`、`apps/dsa-web/src/pages/ChatPage.tsx`、`apps/dsa-web/src/types/backtest.ts`、`apps/dsa-web/src/utils/chatExport.ts`、`apps/dsa-web/src/utils/format.ts`、`src/agent/factory.py`、`src/agent/tools/backtest_tools.py`、`src/repositories/backtest_repo.py`、`src/repositories/backtest_run_repo.py`、`src/services/backtest_service.py`、`tests/test_agent_registry.py`、`tests/test_backtest_service.py`、`tests/test_backtest_tools_defaults.py`（18 files, +624/-47）

### Commit 5 — `81adf10` 历史行情来源诊断

> feat: allow explicit history source and expose coverage diagnostics

`load_history_df` 支持 source override，长窗口不复用短缓存，返回 `source_attempts` 便于诊断。`get_daily_history` 最大窗口提升到 1260 天，返回 `coverage_complete/start_date/end_date` 与指标基准，并新增 `get_current_time` 工具。来源失败或不可用不再等同于「更早历史不存在」。

**涉及文件**：`src/agent/tools/data_tools.py`、`src/services/history_loader.py`、`tests/test_data_tools_daily_history_cache.py`、`tests/test_etf_history_coverage.py`、`tests/test_history_loader.py`（5 files, +188/-16）

### Commit 6 — `54ada83` 文档与 Docker 缓存目录

> docs: document Beijing clock, data provenance and backtest evidence chain

更新 CHANGELOG 与完整指南，新增量化可验证性 phase 1 架构说明，并在 Docker 镜像中把 efinance 缓存目录授权给运行时用户。

**涉及文件**：`docker/Dockerfile`、`docs/CHANGELOG.md`、`docs/architecture/quant-verifiability-phase1.md`、`docs/full-guide.md`、`docs/full-guide_EN.md`、`tests/test_scheduler_background.py`（6 files, +112/-1）

---

## 1. 改动清单（按文件）

| 文件 | 类型 | 说明 |
|------|------|------|
| `scripts/docker_e2e.sh` | 新增 | Docker E2E 冒烟测试脚本 |
| `.github/workflows/ci.yml` | 修改 | 新增 `docker-e2e` 作业（`needs: [docker-build]`） |
| `docs/DEPLOY.md` | 修改 | 新增「部署前如何验证镜像可用（Docker E2E 冒烟测试）」FAQ |
| `docs/CHANGELOG.md` | 修改 | `[Unreleased]` 追加多条扁平条目 |
| `data_provider/us_index_mapping.py` | 修复 | `is_us_stock_code` 空值防护 |
| `docs/full-guide.md` / `docs/full-guide_EN.md` | 文档 | 补充「应用时钟固定为北京时间」说明 |
| `tests/test_config_env_compat.py` | 测试 | 屏蔽磁盘 `.env` 干扰 |
| `tests/test_market_analyzer_generate_text.py` | 测试 | 静态检查显式 UTF-8 |
| `tests/test_pipeline_realtime_indicators.py` | 测试 | 实时行情补充 `quote_time` |
| `tests/test_storage.py` | 测试 | 先释放 SQLite 连接再清理临时目录 |

> 注：上表仅列出**未提交**的 working tree 改动。`src/time_utils.py`、`src/core/pipeline.py`、`src/stock_analyzer.py`、`data_provider/realtime_types.py` 等文件已在上面 6 个 commit 中改动（见第 0.1 节），当前工作区无进一步未提交修改。

---

## 2. 详细改动

### 2.1 Docker E2E 冒烟测试（新增）

**文件**：`scripts/docker_e2e.sh`

**做了什么**：
- 构建镜像（默认 `stock-analysis:e2e`）。
- 用临时目录挂载 `data/logs/reports`，以 `python main.py --serve-only --host 0.0.0.0 --port 8000` 启动容器，**不依赖任何外部 LLM / 数据源 / 通知配置**。
- 轮询 `/api/health` 等待就绪（容器提前退出则立即失败并打印日志）。
- 断言以下内容：
  - `GET /api/health` → 200 且含 `"status":"ok"`
  - `GET /api/v1/auth/status` → 200
  - `GET /api/v1/system/config/setup/status` → 200
  - `GET /api/v1/agent/skills` → 200
  - `GET /docs` → 200
  - `GET /api/does-not-exist` → 404 且含 `"error"`
  - `GET /` → 200 且含 `<!doctype html>`（**小写**，grep 需忽略大小写）
  - `index.html` 引用的首个 `/assets/*.js|css` 资源 → 200（防空白页回归）
  - 容器内 `whoami` == `dsa`（非 root）
  - `/app/data` 可写
  - 容器保持运行
- 失败非零退出；`trap cleanup EXIT` 自动清理容器与临时目录。

**可选环境变量**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DOCKER_E2E_IMAGE` | `stock-analysis:e2e` | 镜像名 |
| `DOCKER_E2E_PORT` | `18000` | 宿主机端口 |
| `DOCKER_E2E_KEEP` | `0` | 设为 `1` 保留容器与临时目录排障 |

**运行方式**：
```bash
./scripts/docker_e2e.sh
```

**踩坑记录**：`index.html` 使用小写 `<!doctype html>`，最初用大写 `<!DOCTYPE html>` 断言导致误报失败，已改为小写并额外校验 `/assets/*` 资源真实存在。

---

### 2.2 CI 接入

**文件**：`.github/workflows/ci.yml`

在 `docker-build` 之后新增作业：

```yaml
  docker-e2e:
    name: docker-e2e
    runs-on: ubuntu-latest
    needs: [docker-build]
    steps:
      - name: 📥 Checkout
        uses: actions/checkout@v5
      - name: 🐳 Docker E2E smoke test
        run: ./scripts/docker_e2e.sh
```

---

### 2.3 文档更新

- `docs/DEPLOY.md`：在「常见问题」中新增第 5 条「部署前如何验证镜像可用（Docker E2E 冒烟测试）」，原第 5 条顺延为第 6 条。
- `docs/CHANGELOG.md`：`[Unreleased]` 追加扁平条目（含 Docker E2E、北京时间统一、实时行情时效元信息、`get_current_time` 工具、技术指标 null 语义、`is_us_stock_code` 防护、Windows 测试隔离）。
- `docs/full-guide.md` / `docs/full-guide_EN.md`：补充「应用时钟固定为北京时间（`Asia/Shanghai`）」说明。

---

### 2.4 代码修复

**`data_provider/us_index_mapping.py` — `is_us_stock_code` 空值防护**

```python
# 修复前：code 为 None 时 code.upper() 抛 AttributeError
if code.upper() in _US_STOCK_KNOWN_LIST:
    return True
normalized = (code or '').strip().upper()

# 修复后：先归一化再判断
normalized = (code or '').strip().upper()
if not normalized:
    return False
if normalized in _US_STOCK_KNOWN_LIST:
    return True
```

---

### 2.5 测试隔离修复（Windows 本地）

| 测试文件 | 问题 | 修复 |
|----------|------|------|
| `tests/test_config_env_compat.py` | 磁盘 `.env` 干扰断言 | `@patch.object(Config, "_get_env_file_value", return_value=None)` |
| `tests/test_market_analyzer_generate_text.py` | 默认编码读取源码报错 | `read_text(encoding="utf-8")` |
| `tests/test_pipeline_realtime_indicators.py` | 实时行情缺 `quote_time` 导致时效校验不通过 | 补充 `quote_time=beijing_now().isoformat(...)` |
| `tests/test_storage.py` | Windows 下 SQLite 连接未释放导致临时目录清理 `PermissionError` | 先 `DatabaseManager.reset_instance()` 再 `temp_dir.cleanup()` |

---

## 3. 验证情况

| 项目 | 命令 | 结果 |
|------|------|------|
| Docker E2E 全链路 | `./scripts/docker_e2e.sh` | ✅ 全部 PASS，0 FAIL，退出码 0 |
| 脚本语法 | `bash -n scripts/docker_e2e.sh` | ✅ 通过 |
| CI YAML | 人工核对（本地无 PyYAML） | ✅ 结构与 `docker-build` 一致 |

**Docker E2E 实测输出（关键行）**：
```
[PASS] 镜像构建完成
[PASS] 服务已就绪（第 2 次探测）
[PASS] 根健康检查 /api/health (HTTP 200)
[PASS] 认证状态 /api/v1/auth/status (HTTP 200)
[PASS] 配置 setup 状态 (HTTP 200)
[PASS] Agent 技能列表 /api/v1/agent/skills (HTTP 200)
[PASS] OpenAPI 文档 /docs (HTTP 200)
[PASS] 未知 API 返回 JSON 404 (HTTP 404)
[PASS] 前端首页 / (HTTP 200)
[PASS] 前端资源 /assets/index-DMalAofq.js (HTTP 200)
[PASS] 容器以非 root 用户运行 (dsa)
[PASS] 数据目录 /app/data 可写
[PASS] 容器保持运行
✅ Docker E2E 冒烟测试全部通过
```

---

## 4. 未验证项 / 风险点

- **未验证**：CI 上的 `docker-e2e` 作业尚未在 GitHub Actions 实跑（本地已等价验证）。
- **未验证**：`python scripts/check_ai_assets.py` 在 Windows 本地报 `CLAUDE.md must be a symlink to AGENTS.md`，属既有平台差异（Windows 检出为普通文件），与本次改动无关。
- **风险**：`docker-e2e` 会额外构建一次镜像，增加 CI 时长；已用 `needs: [docker-build]` 串行化。
- **风险**：默认端口 18000，若被占用可用 `DOCKER_E2E_PORT` 调整。

---

## 5. 下一步测试建议

1. **本地回归**（离线）：
   ```bash
   python -m pytest -m "not network" tests/test_config_env_compat.py tests/test_market_analyzer_generate_text.py tests/test_pipeline_realtime_indicators.py tests/test_storage.py
   ```
2. **后端门禁**：
   ```bash
   ./scripts/ci_gate.sh
   ```
3. **Docker E2E 复跑**（含排障保留）：
   ```bash
   DOCKER_E2E_KEEP=1 ./scripts/docker_e2e.sh
   ```
4. **推送后**：观察 GitHub Actions 的 `docker-e2e` 作业是否通过。

---

## 6. 回滚方式

- 删除 `scripts/docker_e2e.sh`。
- 移除 `.github/workflows/ci.yml` 中的 `docker-e2e` 作业。
- 回退 `docs/DEPLOY.md`、`docs/CHANGELOG.md`、`docs/full-guide*.md` 对应条目。
- 回退 `data_provider/us_index_mapping.py` 与 4 个测试文件的改动。

---

## 7. 工作区其他未跟踪文件（非本次改动）

- `full-test.txt`：早前 pytest 输出残留。
- `.agents/`：目录，非本次产物。

> 如需清理请确认后再删除。
