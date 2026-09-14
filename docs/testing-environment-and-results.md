# 本地环境与 Docker 测试记录

> 最新独立复核：见本文 §10（HEAD `43e41f2`）。历史 PASS 记录保留，但新增快照门槛和 CI 调用仍有已复现问题。

记录日期：**2026-09-14，北京时间（Asia/Shanghai，UTC+08:00）**。宿主机当时仍是洛杉矶时间 2026-09-13，本文所有业务日期均以北京时间为准。

本文记录本次代码 review 使用的环境、前一轮离线测试，以及 Docker 恢复后补跑的实际结果。用于复现与交接，不代表生产环境验收完成。

关联文档：[改进计划](quant-improvement-plan.md)、[数据与时间契约设计](architecture/market-data-time-contract.md)、[阶段一设计](architecture/quant-verifiability-phase1.md)。

## 1. 结论与测试边界

新镜像能够启动 Web，关键接口与静态资源可访问，空回测运行证据能落库、校验并在同一容器重启后保留。核心北京时间函数、Agent 时间工具及每日调度器在三种容器时区下结果一致。

**尚不能据此宣称“每天实时数据和所有指标均准确可用”。** 本轮在线检查中，588000 的实时报价超过 40 秒；601398 返回上周五行情且标记 stale。Linux UID 不匹配的目录写入失败也已复现。之前 review 发现的评分、日期过滤、API 元数据透传等问题仍待修复。

| 检查 | 结果 | 证据范围 |
|---|---|---|
| 后端离线测试（前一轮） | 1778 passed，2 deselected | 当前代码工作区；不包含网络标记用例 |
| Web 测试（前一轮） | 398 passed，2 skipped；47 个文件通过 | Vitest；不等于真实浏览器操作 |
| Web 生产构建（前一轮） | 通过 | TypeScript + Vite |
| 本轮 Docker 构建 | 通过 | 当前工作区重新构建；部分依赖层使用缓存 |
| Web / API / 静态资源 | 通过 | 隔离容器，无生产配置 |
| 非 root 用户默认目录写入 | 通过 | UID/GID 1000；镜像默认卷 |
| 空回测证据与重启保留 | 通过 | SHA256 匹配；不是非空收益回测 |
| UTC / 洛杉矶 / 上海时区矩阵 | 通过 | 核心时钟、Agent 时间、调度器配置与下次运行时间 |
| Linux 目录属主不匹配 | **复现 PermissionError** | 目录 UID 1001、0755；进程 UID 1000 |
| 588000 / 601398 历史 750 条 | 两者成功 | 显式使用 YfinanceFetcher 的本次样本 |
| 588000 实时报价 | **40 秒截止时未返回** | 外层测试截止，不等于接口返回了超时错误 |
| 601398 实时报价 | 返回，但 **stale** | 行情时间 2026-09-11；拉取时间 2026-09-14 |

## 2. 代码与环境身份

### 2.1 代码快照

- 仓库：`E:\Projects\daily_stock_analysis`。
- HEAD：`54ada83fa39f0722554e22c03aeda2e728060bad`。
- 本轮测试使用 **HEAD + 未提交工作区**，不是干净提交。工作区已有其他工具修改的 CI、数据映射、测试、部署文档及未跟踪 E2E 脚本。
- Docker 验证开始时 tracked diff 的 SHA256：`d38ce35ac6309bd0f0cec0140a5c8099099f1993e2dc14ec743b259d6d4eca3f`。
- 上述 diff 指纹不含未跟踪文件，也不替代完整源代码归档；后续本文及文档索引编辑会改变工作区指纹。
- 本轮未修改业务实现，未提交、打 tag、推送、导出或部署镜像。

### 2.2 宿主机与运行时

| 项目 | 实际环境 |
|---|---|
| 操作系统 / Shell | Windows 11 / PowerShell |
| 宿主机时区 | Pacific Standard Time；测试时实际偏移 UTC-07:00 |
| 业务时区 | Asia/Shanghai，UTC+08:00 |
| 本地 Python | `.venv/Scripts/python.exe`，3.12.14 |
| Node.js | v24.19.0 |
| 本地 npm | 10.9.4，独立下载的 npm CLI |
| Docker Client / Server | 29.4.1 / 29.4.1，Docker Desktop Linux 容器 |
| 镜像 Python | 3.11.16 |
| 镜像用户 | uid=1000(dsa)，gid=1000(dsa) |
| Docker 构建配置 | `docker/Dockerfile`；Node 20 前端构建阶段、Python 3.11 slim bookworm 运行阶段 |

本地 Python 与镜像 Python 版本不同：1778 个离线用例是在本地 Python 3.12 下执行，不能写成“镜像 Python 3.11 全量测试通过”。本轮镜像内执行了下述运行冒烟与定向检查。

本地关键依赖实测版本：pandas 3.0.5、numpy 2.5.3、SQLAlchemy 2.0.52、FastAPI 0.141.1、pytest 9.1.1、AkShare 1.18.94、yfinance 1.7.0、schedule 1.2.2、pytz 2026.3.post1。此列表描述本地虚拟环境，不用于推断缓存镜像中的依赖版本。

## 3. 环境准备与命令

以下 PowerShell 命令从仓库根目录执行。准备依赖会访问包源，需要网络；测试目录与环境不要包含生产密钥。

### 3.1 Python

本机使用的基础解释器路径如下；其他机器替换为自己的 Python 路径。已有可用 `.venv` 时不需要重新创建。

```powershell
$TaskPython = 'C:\Users\lkx14\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $TaskPython -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-ci.txt
.venv/Scripts/python.exe --version
.venv/Scripts/python.exe -m pip freeze
```

`requirements-ci.txt` 引入业务依赖并提供 pytest、flake8。中文诊断脚本使用 `-X utf8`，避免 Windows 默认编码无法输出中文导致测试脚本失败。

### 3.2 Node 与 npm

本机 Node 位于下列目录，初始 PATH 中没有可用 npm。为避免修改系统安装，本轮把 npm 10.9.4 解压到本地忽略目录 `.claude/reviews/npm-runtime/package/`，使用其中的 `bin/npm-cli.js`。

下载来源：`https://registry.npmjs.org/npm/-/npm-10.9.4.tgz`。解压使用 Python `tarfile` 的 `filter='data'`。这属于本次测试环境准备，不是应用新增的运行依赖，也没有提交到仓库。

```powershell
$TaskNodeDir = 'C:\Users\lkx14\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin'
$env:PATH = "$TaskNodeDir;$env:PATH"
$TaskNpmCli = (Resolve-Path '.claude/reviews/npm-runtime/package/bin/npm-cli.js').Path
node --version
node $TaskNpmCli --version
Push-Location apps/dsa-web
node $TaskNpmCli ci
node $TaskNpmCli test
node $TaskNpmCli run build
Pop-Location
```

正常安装了 npm 的机器直接在 `apps/dsa-web` 执行 `npm ci`、`npm test`、`npm run build`。以上 npm 临时目录只在当前电脑存在。Web lint 属于仓库推荐门禁，但本记录没有将它列为已通过项目。

### 3.3 离线测试与诊断

```powershell
.venv/Scripts/python.exe -m pytest -m 'not network' -q --disable-warnings
.venv/Scripts/python.exe -X utf8 .claude/reviews/current-review/reproduce.py
git diff --check
```

前一轮 pytest 用时约 63.70 秒，42 个 warning；Web 测试约 25.86 秒。有 jsdom canvas/scrollTo 提示，构建有大包及 Browserslist 数据陈旧提示。它们没有阻断当次测试和构建。

仓库完整 Linux 后端门禁为 `./scripts/ci_gate.sh`，还包含语法、关键 flake8 和确定性脚本检查。**本记录不把单独的 pytest 成功等同于整个脚本或远端 CI 成功。** Windows 环境如执行关键 flake8，可使用 `-j 1` 避免进程池权限问题。

根目录 `full-test.txt` 是较早的结果（7 个失败），不是此次 1778 passed 的最新证据。发现未覆盖的业务 bug 与现有测试全部通过并不矛盾。

## 4. Docker 镜像与隔离设置

### 4.1 新镜像

```powershell
docker version
docker build -f docker/Dockerfile -t daily_stock_analysis:codex-review-validation .
docker image inspect daily_stock_analysis:codex-review-validation --format '{{.Id}}'
```

本轮实际构建镜像：`daily_stock_analysis:codex-review-validation`。

镜像身份：`sha256:2fa2c713d3f338c28109cfa007725d8fd962e9d1e168a21ffaba2b2c3de35553`。

没有直接复用旧 `codex-provider-test` 作为当前代码的验收结果。构建使用缓存依赖层并重新构建 Web；不是 `--no-cache` 干净依赖构建。镜像保留在本机供检查，未替换用户其他镜像。

### 4.2 Web 启动方式

本次隔离启动配置如下；容器名称由测试脚本随机生成。这里使用可读示例名称，避免与正式服务混淆。

```powershell
docker run --rm -d --name dsa-review-example -p 127.0.0.1::8000 -e ADMIN_AUTH_ENABLED=false -e SCHEDULE_ENABLED=false daily_stock_analysis:codex-review-validation python main.py --serve-only --host 0.0.0.0 --port 8000
docker port dsa-review-example 8000/tcp
```

- `--serve-only` 是启动 Web 的关键；镜像默认命令是定时模式，单独映射端口不会自动启用 Web。
- `127.0.0.1::8000` 只向本机暴露随机宿主机端口。8000 是这次测试选用的容器端口，不是要求服务器固定使用的端口。
- 关闭认证仅用于本次本机隔离检查，不能据此照搬生产安全配置。
- 未传入生产 `.env`、Tushare Token、LLM Key 或通知配置；未触发分析和通知发送。
- 未挂载生产数据库。使用镜像声明的数据、日志、报告匿名卷，删除 `--rm` 容器时清理。
- 在线探测使用独立容器，只把本地探测脚本以只读方式挂载进去。

## 5. Docker 实测过程与结果

### 5.1 Web 与文件权限

最终完整运行开始时间：`2026-09-14T09:09:08.572130+08:00`。

以下路径返回 HTTP 200：`/api/health`、`/api/v1/auth/status`、`/api/v1/system/config/setup/status`、`/api/v1/agent/skills`、`/docs`。不存在的 `/api/does-not-exist` 返回 404 JSON 错误。

首页返回 HTML，其中引用的 `/assets/index-C_Zwi03m.js` 和 `/assets/index-DlsoPncp.css` 均可访问。这验证静态产物打包与服务，不包含真实浏览器点击、图表显示或对话联调。

以 UID 1000 写入并删除测试文件，以下目录通过：

- `/app/data`
- `/app/logs`
- `/app/reports`
- `/usr/local/lib/python3.11/site-packages/efinance/data`

### 5.2 回测证据落库与重启

1. 对隔离空数据库 POST `/api/v1/backtest/run`，参数 `{"limit":1}`。
2. 校验 `status=empty`、`processed=0`，并取得独立 run_id。
3. GET `/api/v1/backtest/runs/{run_id}`，对 evidence 按 `ensure_ascii=False, sort_keys=True, separators=(',', ':')` 序列化后计算 SHA256，与接口字段比对。
4. 重启同一容器，重新读取端口，再访问证据接口；内容与重启前完全一致。

最终 run_id：`2ac0ac8a1980493ea7e90efce346fd91`。该记录随隔离测试数据清理，不属于生产记录。

边界：验证了空运行审计和同一容器重启保留；没有验证非空策略收益、三年组合回测、容器删除重建后的外部持久卷恢复或数据库升级。

### 5.3 三种时区

通过 `docker run --rm -e TZ=<时区> --entrypoint python <镜像> -c <探测代码>` 分别检查 `beijing_now()`、Agent `_handle_get_current_time()` 和 `Scheduler(schedule_time='18:00')`。使用 `run_immediately=False`，不执行业务任务。

| 容器 TZ | 容器原生时间（约） | 应用北京时间（约） | 下次执行时间 |
|---|---|---|---|
| UTC | 09-14 01:09:14 | 09-14 09:09:14 +08:00 | 09-14 18:00:00 +0800 |
| America/Los_Angeles | 09-13 18:09:14 | 09-14 09:09:14 +08:00 | 09-14 18:00:00 +0800 |
| Asia/Shanghai | 09-14 09:09:15 | 09-14 09:09:15 +08:00 | 09-14 18:00:00 +0800 |

三者应用偏移均为 28800 秒，调度时区均为 Asia/Shanghai。没有等待到 18:00 实际触发，也没有模拟夏令时切换、跨午夜、任务/新闻/持仓等所有服务。之前发现的剩余宿主机时钟问题仍然成立。

### 5.4 Linux UID 权限负例

创建独立命名卷，以 root 将测试目录设置为 UID/GID 1001、0755，并放入一个占位文件；随后以镜像默认 UID 1000 挂载到 `/app/data`，尝试创建 `uid_test`。

实际结果：退出码 1，`PermissionError: [Errno 13] Permission denied: '/app/data/uid_test'`。

这确认了不同属主、不可写目录会阻止非 root 容器写入。`scripts/docker_e2e.sh` 使用宿主机临时目录，Linux runner 属主与容器 UID 不同时存在这一风险。**这不是已经在 GitHub runner 上运行整个脚本得到的失败结果**；本轮用 Linux 容器内命名卷模拟了相同 Unix 权限条件。

原脚本在镜像内通过 `bash -n` 语法检查，未在 Windows 上原样执行完整 Bash E2E。下一步应修正临时目录初始化，并在目标 Linux runner 执行原脚本。

### 5.5 测试脚本自身的两次修正

第一次重启检查失败：Docker 重启后随机发布端口改变，脚本还在访问旧地址。第二次实际观察到 `60998 → 63388`；最终运行观察到 `50638 → 50653`。修正为重启后重新执行 `docker port`，健康与证据检查通过。此处是测试寻址问题，不是应用启动失败。

第二次权限负例未按预期失败：空命名卷存在 Docker 自动填充镜像目录的干扰，最初的夹具不足以稳定保留预设属主条件。补充占位文件后，负例稳定返回 PermissionError。保留两次未完成的原始结果，不将它们伪装成首次即全通过。

## 6. 在线行情探测

探测约发生在北京时间 **2026-09-14 09:05，盘前**。每个子请求设 40 秒外层截止，容器再设 55 秒上限；两个标的的历史与实时报价共四个任务并发。没有使用付费 API Key。

历史请求明确调用 `_handle_get_daily_history(code, days=750, source='YfinanceFetcher')`。因此本轮证明的是显式切到该来源可取得这些样本，**没有验证 AkShare ETF 接口当时的可用性，也没有验证自动 fallback 的整个顺序**。

| 标的 | 返回来源 | 条数 | 开始日期 | 结束日期 | coverage_complete |
|---|---|---:|---|---|---|
| 588000 | Yahoo | 750 | 2023-08-09 | 2026-09-11 | true |
| 601398 | Yahoo | 750 | 2023-08-10 | 2026-09-11 | true |

这次结果说明 588000 并非所有来源都只能提供 169 条。`coverage_complete=true` 仍只是当前接口条数口径，不证明交易日无缺口、复权一致或精确覆盖某个三年自然日期区间。

实时报价调用 `_handle_get_realtime_quote(code)`，走应用实际路由：

| 项目 | 588000 | 601398 |
|---|---|---|
| 结果 | 外层 40 秒超时 | 返回成功 |
| source | 未取得 | tencent |
| price | 未取得 | 8.11 |
| quote_time | 未取得 | 2026-09-11T16:15:00+08:00 |
| fetched_at / current_time | 未取得 | 2026-09-14T09:05:23+08:00 |
| freshness | 未取得 | stale |
| volume / unit | 未取得 | 360705400 / shares |
| volume_ratio | 未取得 | 1.39 |
| turnover_rate | 未取得 | 0.13 |

601398 的字段有真实接口返回，但本轮没有独立交易所数据交叉核验其数值。盘前取到上周五行情不等于供应商故障，关键是保留其行情日期，不能当作当天盘中成交数据。588000 超时未单独定位到某一家供应商，不能直接归因于收费或缺 Key。

## 7. 已知问题与下一步验收

1. **先修指标有效性与评分。** 缺成交量、短样本时，默认枚举仍可能加分，风险列表还可能被覆盖；输入 NaN 也可能被指标运算掩盖。MA、量比、RSI、MACD 需要固定样本公式校验及缺口负例，不应以接口有值为正确标准。
2. **补日期与 API 契约。** 短窗口历史请求的截止日期、实时接口丢失来源/时效元数据、剩余宿主机时间调用，继续按计划 WP2–WP4 处理。
3. **修 Docker 权限与原脚本验收。** 明确 UID/目录初始化，增加“宿主机目录 UID 与容器不同”的回归用例；不要使用全局放宽权限掩盖问题。
4. **补盘中与收盘后的实时验证。** 分别检查源行情时间、拉取时间、交易日预期、超时预算和失败分类；把每日可用性保存成运行记录，再讨论“每天稳定”。本次未设置后台持续监控。
5. **补非空回测与真实浏览器验收。** 使用固定输入快照验证数值、重放和 Web 证据查看，再进行组合回测设计的后续阶段。

## 8. 证据文件、清理与回滚

本机原始产物位于 `.claude/reviews/`，按仓库规则默认忽略，不会随普通代码提交分发：

- `current-review/REVIEW.md`：前一轮详细审查。
- `current-review/reproduce.py`：R1–R5 的确定性复现。
- `docker-validation/validate.py`：本轮 Docker 验证脚本。
- `docker-validation/results.json`：最终完整结果、镜像身份、时区输出、负例与清理状态。
- `docker-validation/results-first-attempt.json`、`results-second-attempt.json`：两次未完成尝试。
- `docker-validation/network_probe.py`：在线探测脚本；关键返回已固化在本文，原始标准输出见本次会话。

在当前电脑复跑最终 Docker 验证：

```powershell
.venv/Scripts/python.exe -X utf8 .claude/reviews/docker-validation/validate.py
```

它使用随机测试容器/卷名称并在结束时清理，不挂载生产数据。脚本不是已入库的团队长期门禁；其他电脑应使用本文步骤或后续修正后的 `scripts/docker_e2e.sh`，不能假设忽略目录存在。

最终临时 Web 容器与权限测试命名卷均清理成功（退出码 0），在线容器自动删除；另行查询未发现 `dsa-review` 测试容器或命名卷残留。新镜像保留，可在不再需要时精确删除测试标签：

```powershell
docker image rm daily_stock_analysis:codex-review-validation
```

本次交付是文档与本地验证产物，没有业务代码迁移或生产数据变更。回滚只需撤销本次文档增量、按需删除专用测试镜像，不应重置整个工作区或删除用户已有镜像/卷。中文专项测试记录不新增英文副本；公共中英指南本轮未改变产品行为说明。

## 9. WP1/WP2/WP3 修复后的 Docker 复验（2026-09-14）

镜像：`stock-analysis:wp-verify`（当前工作区源码构建，Linux 容器）。三层验证全部通过。

### 9.1 容器 E2E 冒烟（服务链路）

`DOCKER_E2E_IMAGE=stock-analysis:wp-verify ./scripts/docker_e2e.sh` → 退出码 0，14 项断言全部 PASS：镜像构建、数据卷属主初始化为 `1000:1000`、服务就绪、`/api/health`、`/api/v1/auth/status`、`/api/v1/system/config/setup/status`、`/api/v1/agent/skills`、`/docs`、未知 API 返回 JSON 404、前端首页与 `/assets/index-*.js`、非 root 用户 `dsa`、`/app/data`+`/app/logs`+`/app/reports` 均可写、容器保持运行。

### 9.2 WP1/WP2/WP3 容器内离线探测

探针：`.claude/reviews/wp-verify/probe.py`（本地产物，不入库）。使用合成行情的确定性样本，不访问网络、不调用 LLM；其中 WP3 的 response_model 检查通过离线解析 `app.openapi()` 完成，不需要行情网络。

```powershell
docker run --rm -e TZ=<UTC|America/Los_Angeles|Asia/Shanghai> -e PYTHONPATH=/app -w /app `
  --mount type=bind,source=<abs>/probe.py,target=/probe/probe.py,readonly `
  --entrypoint python stock-analysis:wp-verify /probe/probe.py
```

三种时区均为 `RC=0` 且 `SUMMARY {"failed": [], "total": 28}`：

| 检查 | 本轮结果 |
|---|---|
| `wp1_missing_indicators_not_actionable` | score 54 / 持有 / `insufficient_data`（不再是 77 分强烈买入） |
| `wp1_risk_factors_preserved` | MA60、前5日成交量、MACD、RSI 缺口与「不生成可执行买卖信号」全部保留 |
| `wp1_price_gap_invalid` | `rsi`/`macd` 判为 `invalid`，输出 null |
| `wp1_partial_volume_window_consistent` | 分析器 `None` 与基础指标 `nan` 一致判为不足 |
| `wp1_complete_data_score_stable` | 完整 60 行得 72 分、`买入`、全部指标 `valid`（权重未漂移） |
| `wp1_json_strict_finite` | `json.dumps(..., allow_nan=False)` 通过 |
| `wp2_r2_future_rows_not_returned` | 请求 `2024-01-31` 返回 0 行；kwargs 为 `start_date=2023-10-05, end_date=2024-01-31, min_records=None` |
| `wp2_r4_app_clock_is_beijing` | 三时区下 offset 均为 28800，应用时钟与北京时间同一小时/日期 |
| `wp2_r4_news_date_normalized_to_beijing` | `2026-03-15T23:30:00Z` → `2026-03-16`（三时区一致） |
| `wp2_r4_migrated_modules_use_beijing_clock` | 14 个迁移模块均无裸 `datetime.now()` / `date.today()` |
| `wp2_r4_exclusions_unchanged` | `trading_calendar` fail-open 与 `bot/platforms/*` 签名时钟保持原语义 |
| `wp3_schema_declares_quote_evidence` | `StockQuote` 声明全部 11 个证据字段 |
| `wp3_response_model_exposes_evidence` | 200 响应 `$ref` 指向 `StockQuote` 且字段齐备（防 `response_model` 静默剥离） |
| `wp3_stale_quote_keeps_source_time_and_freshness` | 2024 报价仍保留 `quote_time=2024-01-01T15:00:00+08:00`、`freshness=stale`、`is_realtime=false`、`source=tencent`、`session_date=2024-01-01`，`update_time` 等于 `fetched_at` 且带 +08:00 |
| `wp3_us_session_date_uses_exchange_timezone` | 北京时间 09-14 05:00 的美股报价 `session_date=2026-09-13`（按交易所时区） |
| `wp3b_failure_categories` | 8 类失败映射齐备：timeout / network / rate_limit / auth / permission / unsupported / invalid_payload / unknown |
| `wp3b_timeout_not_treated_as_absence` | 单源超时 → 抛 `DataFetchError`（不是空数据），attempts 记录 `status=error, reason=timeout` |
| `wp3b_explicit_source_no_silent_fallback` | 指定来源失败时其它来源**未被调用**，attempts 只含被指定的来源且 `reason=network` |
| `wp3b_loader_surfaces_failure_reason` | 加载器把失败原因通过 `source_attempts` 暴露给调用方（超时不被当作「没有历史」） |
| `wp4_snapshot_identity_differs_by_adjustment` | 同源同区间不同复权 → 不同 `snapshot_id`，`payload_hash` 相同 |
| `wp4_unknown_unit_not_strategy_eligible` | 单位未知 → `partial`、`input_eligibility=false`，`ensure_input_eligible()` 抛错 |
| `wp4_three_year_request_not_marked_complete` | 请求近三年但只有约一个月数据 → `coverage_complete=false` 且非 `verified` |
| `wp4_create_is_idempotent` | 重复写入同参数返回同一 `snapshot_id` 且 `created_at` 不变 |
| `wp4b_strategy_engine_requires_eligible_snapshot` | 策略引擎缺 `snapshot_id` 或引用不合格快照 → 入口抛 `ValueError` |
| `wp4b_run_records_snapshot_reference` | 合格快照运行记录含 `snapshot_id`/`verified`/`input_eligibility=true`/`engine_kind=portfolio_daily`，证据含 `snapshot` 块 |
| `wp4b_report_evaluation_stays_backward_compatible` | 报告评估无快照时 `snapshot_id=null`、`engine_kind=ai_report_evaluation`（旧语义不变） |
| `wp4c_readonly_snapshot_api` | 列表不含 `bars`、详情按需返回 5 行明细且 `input_eligibility=true`，schema 字段齐备 |
| `wp4c_payload_budget_enforced` | 单份 payload 超预算抛 `ValueError` 且不入库；`storage_stats()` 计数与 `within_budget` 正确 |
| `wp4c_agent_tool_readonly` | Agent 工具列表/详情/不存在分支正确，且**不新增快照**（只读） |

### 9.3 三时区调度/时钟矩阵

```powershell
docker run --rm -e TZ=<tz> -e PYTHONPATH=/app -w /app `
  --mount type=bind,source=<abs>/clock_matrix.py,target=/probe/clock_matrix.py,readonly `
  --entrypoint python stock-analysis:wp-verify /probe/clock_matrix.py
```

UTC / America/Los_Angeles / Asia/Shanghai 三者输出一致：`offset_seconds=28800`、`schedule_timezone=Asia/Shanghai`、`schedule_next_beijing=2026-09-14 18:00:00 +0800`、`tool_current_time=2026-09-14T09:54:26+08:00`。

### 9.4 全量离线套件（修复后，本地实跑）

在 WP1/WP2/WP3 与 R6 改动全部落盘后，于仓库根目录执行：

```powershell
.venv\Scripts\python.exe -m pytest -m "not network" -q
```

结果：**1798 passed, 2 deselected, 42 warnings in 58.69s**，**0 failed / 0 error**。覆盖本次新增的 `test_stock_analyzer_indicator_validity.py`（11）、`test_stock_quote_evidence.py`（8）与更新后的 `test_history_loader.py` / `test_data_tools_daily_history_cache.py` / `test_search_news_freshness.py` / `test_search_tavily_provider.py` / `test_portfolio_service.py`。

补充说明：本机不同次运行观察到的**收集数**在 1798–1954 之间波动（pytest 临时目录/缓存目录参与 `testpaths = .` 收集的差异），**各次均为 0 failed / 0 error**；判定标准以失败数为准，收集数不作为验收指标。

### 9.5 WP4 收尾与前端快照展示（本地 + 容器实跑）

改动面：快照覆盖判定（交易日历 session 数 + 判定依据披露）、只读快照导出工具 `scripts/export_market_snapshots.py`、Web 回测卡片展示快照引用与覆盖依据。

- flake8 关键检查：`flake8 . --count --select=E9,F63,F7,F82`（CI 同口径）**0 问题，rc=0**。
- 受影响面后端回归（21 个测试文件，`-m "not network"`）：**274 passed, rc=0**，含快照冻结、只读快照 API、导出工具、交易日历、大盘复盘、API 契约与静态产物一致性。
- 新增/扩充回归：`tests/test_market_snapshot_session_coverage.py`（17 项）、`tests/test_export_market_snapshots.py`（7 项）；Web `BacktestRunCard.test.tsx` 由 8 项增至 10 项。

前端工具链在本机**没有 Node 运行时**（PATH、`Program Files`、`E:\Tools`、`.vscode\extensions`、nvm 均无 `node.exe`；VS Code 的 Electron 以 `ELECTRON_RUN_AS_NODE=1` 调用无输出），因此改在 Linux `node:20` 容器中对前端源码副本执行与 CI `web-gate` 相同的命令：

```bash
docker run --rm -v <stage>:/app -w /app node:20 sh -c \
  'npm ci && npm run lint && npm run build && npm run test'
```

实测：`npm ci` rc=0（466 包）；`npm run lint` rc=0；`npm run build` rc=0（`tsc -b` 类型检查 + vite build，3180 modules）；`npm run test` rc=0 → **Test Files 47 passed (47)、Tests 405 passed | 2 skipped**，其中 `BacktestRunCard.test.tsx (10 tests)` 全绿。

注意：首次容器运行被中断，其日志中的 `31 files / 272 tests` 属**未跑完**的中间结果，不作为证据；以完整跑完的 47/405 为准。

**CI 现状（重要）**：`web-gate` 只执行 `npm ci` + `npm run lint` + `npm run build`，**不执行 `npm run test`**，`scripts/ci_gate.sh` 也不含任何 Node 步骤。仓库内 47 个前端测试文件此前从未在 CI 执行，本轮为首次全量跑通；本分支已把 `npm run test` 加入 `web-gate`（该 job 尚未在 GitHub runner 上跑过）。

### 9.5.1 Docker E2E 复跑（本分支代码，Windows/Docker Desktop）

```bash
bash scripts/docker_e2e.sh
```

结果：**14 项断言全 PASS**。镜像为多阶段构建，前端在镜像内执行 `npm ci` + `npm run build`（即 `tsc -b` 类型检查 + vite 打包），因此这条链路同时验证了新前端代码可在镜像内构建：

- 镜像构建完成；`.dockerignore` 生效后构建上下文仅 1.02 kB（代码层 354.96 kB）；
- 命名卷属主初始化为 `1000:1000`（R6 的本地验证路径）；
- 服务就绪后 `/api/health`、`/api/v1/auth/status`、`/api/v1/system/config/setup/status`、`/api/v1/agent/skills`、`/docs` 均 200，未知 API 返回 JSON 404；
- 首页 200 且其引用的 `/assets/index-Dia7QUsN.js` 可访问（防空白页回归）；
- 容器以 `dsa` 运行，`/app/data`、`/app/logs`、`/app/reports` 在 UID/GID 1000 下可写，测试结束时容器仍在运行。

边界：仍是 Windows/Docker Desktop 上的验证；R6 关注的「Linux runner 调用者 UID≠1000」只有在 CI 的 `docker-e2e` job（本分支新增）首次执行后才能算被证明。


### 9.6 本轮边界

- 仍为**离线确定性**验证：不验证第三方行情真实性、模型回答正确性或目标服务器在线可用性。
- 未覆盖跨北京时间午夜、周末、节假日与美股夏令时的端到端场景（属计划 WP5 的测试矩阵）。
- 第 5.4 节的 Linux UID 权限负例（**R6 已修复**）：`scripts/docker_e2e.sh` 不再使用调用者创建的 bind mount 目录，改为 Docker 命名卷并在启动前以 root 显式把属主初始化为 `1000:1000`，同时把可写校验扩展到 `data/logs/reports` 三个目录。本轮在 Windows/Docker Desktop 上复跑通过；**尚未在真实 Linux runner（UID≠1000）上执行**，因此「Linux CI 上一定通过」仍未被直接证明。

## 10. HEAD 43e41f2 独立复核（2026-09-14 12:26，北京时间）

审查范围 `54ada83..43e41f2`，7 个新提交、71 个文件。开始时 Git 可见工作区干净。以下是本次独立实测，不与上面的历史验证混成一次运行。

### 10.1 本轮结果

| 检查 | 结果 | 边界 |
|---|---|---|
| 完整离线后端 | **1858 passed，2 deselected，42 warnings，60.14 秒** | 本地 Python 3.12.14 |
| Web 单测 | **47 文件通过，405 passed，2 skipped，10.53 秒** | 本地 Node 24，已安装依赖 |
| Web tsc + Vite build | 通过，3180 modules | 有大包与 Browserslist 陈旧提示 |
| 旧评分/日期/报价探针 | 修复有效 | mock 数据，非在线行情 |
| 新快照负例 | 复现无效输入仍 verified | NaN、无价格、重复日期 |
| required_rows 提高 | 仍复用旧 eligible=true | 5 条要求提高到 100 条 |
| 新引擎分派 | 仍调用旧报告引擎 | 接受不同标的快照并记录 portfolio_daily |
| Linux CI 启动权限 | **退出 126，Permission denied** | Git mode 0644；CI 直接执行 |
| 导出 CLI --help | **退出 1，ModuleNotFoundError: src** | 无额外 PYTHONPATH |
| sync_agent_skills --check | OK，4 files in sync | 本地检查 |
| check_ai_assets | Windows CLAUDE.md 非软链，失败 | 不推断 Linux 同样失败 |

### 10.2 环境与复现

完整 pytest 首次被旧 `.pytest_cache` 和两个 `pytest-cache-files-*` 目录权限阻断，发生在收集阶段。没有删除或修改它们，仅排除缓存并指定可写 cache_dir 后，全量通过：

```powershell
.venv/Scripts/python.exe -m pytest -m 'not network' -q --disable-warnings --ignore=.pytest_cache --ignore-glob='pytest-cache-files-*' -o cache_dir=.claude/reviews/latest-review/pytest-cache
```

前端在 `apps/dsa-web` 沿用 §3 的临时 npm CLI：

```powershell
node ../../.claude/reviews/npm-runtime/package/bin/npm-cli.js test
node ../../.claude/reviews/npm-runtime/package/bin/npm-cli.js run build
```

本轮没有重新 npm ci、lint 或构建业务镜像，不将上述结果称为完整 CI 绿灯。

Linux 权限探针使用已有 `stock-analysis:wp-verify` 作为 Bash/Python 环境；输入当前 HEAD 的 `git archive`，保留脚本 0644 权限，执行 CI 同款 `./scripts/docker_e2e.sh`，退出 126。没有进入 E2E 脚本体，不涉及宿主 UID，也不否定此前 `bash scripts/docker_e2e.sh` 的成功；两种调用方式不同。隔离容器 --rm 自动清理。

### 10.3 已复现问题与建议

1. **P1，CI 入口：** `.github/workflows/ci.yml:112` 直接执行 Git mode 100644 的脚本。应使用 bash 或提交 executable bit，并验证干净 checkout。
2. **P1，快照质量：** `market_snapshot_repo.py:270` 开始的创建路径没有验证必需价格、非有限值和唯一日期；以原始行数判断覆盖。NaN 收盘价、完全无价格、重复日期三组均取得 verified / input_eligibility=true。应按合法字段和请求区间内有效唯一 session 核验；存储时将 NaN 转 null 不能代替质量校验。
3. **P1，执行证据：** `backtest_service.py:102` 接受 portfolio_daily 和快照引用后仍调用旧报告引擎，未把冻结 bars 传给计算，也未校验标的一致。应拒绝未实现引擎，并在实现后绑定实际输入。普通 HTTP run 入口尚未暴露这两个参数，漏洞首先位于新增服务层。分派探针使用 mock 引擎返回，不是一次实际收益回测。
4. **P2，资格复用：** required_rows 影响质量却未进入评估身份；同样 5 条数据先按 5 条创建，再要求 100 条，仍取回原合格状态。应把资格与本次运行要求/质量规则版本绑定，不能通用地复用首次布尔值。
5. **P2，导出入口：** 根目录执行 `python scripts/export_market_snapshots.py --help` 无法导入 src。函数测试未覆盖子进程入口；应修正启动方式并补无 PYTHONPATH 的 CLI smoke。

旧问题复测：缺指标由 77 分强买变为 54 分持有且保留风险；价格缺口下 MACD/RSI 不再给出有效数值；历史目标日后的数据被裁剪；报价 payload 保留 stale、源时间及字段来源。旧修复有实质效果，新增门槛仍需补强。

CHANGES_SUMMARY 仍有漂移：顶部写 6 个新提交和 13 项 Docker PASS，正文 HEAD 写 24ff6eb；实际为 43e41f2、7 个新提交、相对 f76e8c7 共 13 个提交。§9.5.1 的 14 项 PASS 不覆盖本次 CI 调用负例。本轮只记录差异，没有改写用户历史交付声明。

### 10.4 交付边界

详细审查、确定性负例和 Linux 权限脚本位于本地忽略目录 `.claude/reviews/latest-review/`：`REVIEW.md`、`probe.py`、`probe-results.json`、`ci_mode_probe.py`。团队交接以本文为持久记录，忽略目录不会自动随 Git 分发。

本轮未调用真实行情、付费 API 或 LLM，未做真实浏览器联调、全天调度、远端 CI 或生产部署。没有修改业务代码，仅追加审查/测试文档及本地产物。回滚只处理本轮文档增量，不重置用户提交。中文专项记录不新增英文副本，公共双语产品文档未改变。
