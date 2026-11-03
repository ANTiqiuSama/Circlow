# CodexQuotaTray · AI 额度与 TPS 托盘

面向 Windows 和 macOS 的本机 AI 额度与 TPS 工具。Windows 版把来源图标放到任务栏通知区域，悬停展开任务面板；macOS 版把各来源图标放到屏幕顶部菜单栏，点击图标查看额度、重置倒计时、TPS 和刷新操作。两版共用额度与本机记录读取器。

A Windows system-tray monitor for local AI quota and token-usage signals, with generation-rate indicators, reset countdowns and topmost detail cards.

[MIT 许可证](LICENSE) · [快速开始](#安装与启动) · [数据源](#数据源与支持范围) · [配置](#配置) · [构建](#构建-exe) · [自检](#自检与排错)

## 功能概览

- **独立来源图标**：默认显示 Codex、Grok、Gemini，Claude Code 为可选来源。
- **额度圆环**：沿用深色圆盘和来源字母。有两个窗口时外圈分为左右两段，左侧为短窗口、右侧为长窗口；Gemini 有三个模型池时外圈均分为 120°/120°/120° 三段（Argon 左上、Claude/GPT 底部、Gemini 右上，每段从自身中点向两侧填充），只有两个池时分为左右两段。单窗口仍为完整圆环；生成时显示 TPS 数字。
- **接近用尽与重置**：Claude Code、Codex、Grok 的小时或周额度，以及 Gemini 自己的模型池（不含 Argon 和 Claude/GPT），剩余低于 15% 时按耗尽处理；其他来源与额度池（Argon、Claude/GPT、MiMo）剩余不超过 5% 时按耗尽处理。已知重置时间时托盘把额度圆环换成红色倒计时（下次重置还有多久），右上角 `d`、`h`、`m` 分别表示天、小时、分钟。Claude Code、Codex、Grok 达到阈值后倒计时优先于 TPS 显示，其他来源有生成速率时优先显示 TPS；Gemini 不在界面显示 TPS。面板保留真实百分比并标注接近用尽，旧数据会在面板中提示。Gemini 模型池低于 15% 时直接显示它的重置倒计时，其他模型池仅在全部已知池均达到阈值时显示等待时间。Codex 折线图在 5 小时额度剩余不超过 10% 且仍在工作时才改画每周额度。
- **生成速率显示**：取得可核验样本且来源处于活动状态时，圆盘点亮并显示最近样本；缺少样本时继续显示额度。
- **Agent 任务气泡**：悬停任一额度图标都会打开任务面板（没有任务时也显示）：顶部保留各额度窗口与 TPS，每列标题直接写距下次重置的时间（如 4小时32分钟、3天7小时、323天；尚未开始计时的窗口仍写窗口名），下方每个气泡是一项任务及其当前动作；把气泡拖到一起即告诉程序它们是同一任务的并行部分，合并为大气泡并重新识别。
- **按需刷新**：根据空闲、活动、生成结束、读取失败和重置时间调度额度查询。
- **额度耗尽预测**：从本程序积累的数值历史估计消耗趋势，样本不足时明确提示。
- **全局额度趋势**：悬停任一来源时，折线图都画出每一个已有额度读数的模型，不限于正在工作或最近使用的来源。Gemini 的 Argon、Claude/GPT、Gemini 三个额度池都入图。同一来源的多个 agent 共用账号额度，合为相同窗口的曲线。Codex、Claude Code 仍优先画 5 小时窗口。
- **自动缩放时间轴**：折线图按最近的消耗速度选择显示多长：在 10 分钟 / 30 分钟 / 1 小时 / 2 小时 / 5 小时中取最短的、能看到某个额度用掉至少 3% 的一档，所以高强度使用时是 10 分钟的特写，消耗慢时自动拉长；10 分钟的特写在悬停约 2.5 秒后用 1.2 秒平滑拉远到 30 分钟：缓动曲线是五次 smootherstep（起止时速度和加速度都为零，不会猛地启动或骤停），跨度按固定倍率增长（10→17→30 分钟），视觉上缩放速度均匀；纵轴比例在两档各自的刻度之间沿同一曲线过渡，旧读数进入视野时不会跳变；期间横轴刻度直接用 30 分钟一档的间隔，不逐帧跳动。已是 30 分钟及以上的不变；几乎没有消耗时显示已记录的全部（最多 5 小时）。重置时回升到 100% 不算消耗；按读数间隔折算落在该时段内的部分。已在显示的一档只要仍能看到其中六成的消耗就保持不变，避免在相邻两档间来回跳。图下方按跨度在整点时刻标出时间（10 分钟每 2 分钟、30 分钟每 5 分钟、1 小时每 15 分钟、2 小时每 30 分钟、5 小时每小时），“现在”在其刻度处以较亮的字标出，离“现在”太近的整点不再重复标注；短跨度更频繁地重绘（10 分钟一档每 10 秒，最长每 30 秒），曲线随时间平滑推进；“现在”右侧只留一条窄预测区（近景约 1 分钟）画虚线，虚线从最后一个读数接着画，较远的用完或重置时间只写在文字里，不再把横轴拉长到右侧大片空白。
- **完整曲线**：曲线数据是最近 5 小时的全部读数，不只当前窗口：重置前的曲线照常画到重置时刻，在重置处回到 100%，新窗口从开始使用时的 100% 接着画；读数保存在本机 `history.json`，重启后照常调用。预测仍只按当前窗口的读数计算。连续相同的读数只保留第一个和最新一个，悬停时的高频读取不会把更早的曲线挤出，每个窗口最多保留 2000 个读数、8 天。
- **正在使用与空闲**：图中画出每个有读数的额度；正在工作或最近 30 分钟有输出的来源（Gemini 按实际用到的模型池）是实线、带填充和预测虚线，纵轴比例也只按它们取；其余来源只画一条细而淡的线作参照，没有填充、预测、重置标记或用完提示。图例先列正在使用的来源（全名），空闲来源用简称排在后面，只占第一行剩下的位置，不会挤出第二行；只有一个来源在使用时，第一行右端照常显示它“约 X 用完”或下次重置的时间。
- **一次性异常读数**：同一重置窗口内，前后两个读数一致、夹在中间的至多 3 个读数却回升 5 个百分点以上或属于另一个重置窗口（例如空闲 Claude 会话报出的 100%，或 Grok 一次读到别的窗口），或单个读数比两侧都高出 5 个百分点，视为读数故障，不画进曲线也不参与预测；旧版本存下的这类读数在启动时清掉。真正的重置之后读数停留在新窗口，不受影响；最新一个读数要等下一个读数到来才判断。
- **趋势窗口选择**：Codex、Claude Code 有 5 小时窗口时优先绘制该窗口。Codex 的 5 小时额度耗尽但会话仍在工作时，曲线改用每周窗口并标注“5 小时已用完”；5 小时额度恢复后自动切回。
- **自适应趋势刻度**：纵轴底部固定为 0%（用完线），顶部取不低于当前最高读数的整数档位；中间按距顶部的深度做类似 matplotlib `asinh`/`symlog` 的变换（靠近顶部近似线性、往下渐变为对数），过渡宽度按各条曲线的变化幅度自动选取，让变化最小的曲线也能占到足够高度，平稳的读数不被放大。刻度标注剩余百分比，不再显示“log”标记。横轴只画最近 30 分钟的折线及按这段长度延伸的近期预测，避免较长周期将当前变化挤到边缘。实线和预测虚线共用同一刻度，原始额度与耗尽时间计算保持不变。
- **缓存与诊断**：失败时保留最后有效数值并标记旧数据，运行日志自动轮转。

### 图标与面板示例

以下图片由项目自身的绘制代码生成，使用演示数据，不代表真实账号额度、实测速率或当前运行状态。图标依次展示 C、X、G、A、M（依次对应 Codex、Grok、Gemini、Claude、MiMo）；A 为可选的 Claude Code 来源。

![深浅背景下的图标演示，包含 16、24、64 像素尺寸](assets/icons-demo.png)

![使用演示数据生成的任务面板（`python demo_assets.py` 重新生成）](assets/panel-demo.png)

## 数据源与支持范围

| 图标 | 来源 | 额度读取方式 | 生成速率与输出计数 | 当前边界 |
| --- | --- | --- | --- | --- |
| C | Codex | 本机 Codex App Server 的 `account/rateLimits/read` | 本地会话的真实输出 token 计数，以及关联的客户端流式时间 | 按账号响应展示一个或两个额度窗口；统计依赖本地日志格式 |
| X | Grok CLI | 临时 CLI 会话执行 `/usage`，解析 `Weekly limit` 与重置时间 | 直接读取客户端原生 `tokens_per_sec` 和输出计数 | 需要对应 CLI 已安装、已登录；终端输出格式变化会影响解析 |
| G | Gemini / Antigravity | `agy --print /usage --output-format json`，读取全部额度池（Argon、Claude/GPT、Gemini），外圈显示剩余最少的两个池 | 读取 Antigravity 桌面端或 CLI 本地记录中的输出计数，按模型回复步骤的创建至完成时间计算 TPS（含首字延迟） | 客户端不提供原生 TPS；`agy` 不能直接替换成任意名为 Gemini 的 CLI |
| A | Claude Code | 原生 status line 同步 5 小时与每周额度 | 本地 `api-request` 与 `requestRef` 关联真实输出计数，计算每次 API 请求的端到端 TPS | Pro/Max 账号；未记录请求开始时间的旧会话只统计输出计数 |
| M | Xiaomi MiMo Token Plan，可选 | 控制台 `/api/v1/tokenPlan/usage`、`/detail`（需登录一次）；或手动 `mimo.json` | 读取 Codex / Claude Code / MiMo Code 本地记录中 `mimo-*` 模型的输入、缓存与输出 token，按价格表换算 Credits | 控制台接口非公开，返回格式依据前端推断，尚缺真实账号验收 |

额度、输出计数和生成速率来自不同信号。能读到额度，不代表同一来源也提供生成 TPS；没有近期记录也不能证明没有使用。

### Claude Code 与 Claude 桌面端

A 来源面向 **Claude Code 的 CLI 和本地会话记录**。Claude 桌面应用的安装状态、免费版聊天额度和桌面端对话记录不通过这条接口读取。

在设置 → **来源**打开 **A · Claude Code**，或在托盘菜单勾选“Claude Code 额度与 TPS”。免费桌面端和 API Key 计费环境没有 Pro/Max 的订阅额度窗口。进程列表分别统计 Claude Code 和 Claude 桌面客户端。

Windows 运行 `CodexQuotaTray.exe --install-claude-statusline` 接入 Claude 官方 status line 数据。它保留其他 Claude 设置，只在没有已有自定义 status line 时安装；当前会话会自动重载，随后把原生 `rate_limits` 同步到本机托盘，不读取 OAuth 凭据、不发送模型请求。脚本只保存会话 ID、采样时间与数值额度，不保存聊天内容。已安装实时接入时不再启动 `/usage` 终端；旧版或未接入环境保留有超时的 `/usage` 兼容读取。`CODEX_QUOTA_PROVIDERS` 和设置页的来源开关仍然有效。

### Xiaomi MiMo Token Plan

Token Plan 按 Credits 计费，本程序统一按**年度**一个额度池计算（周期为套餐到期日前的一年；控制台若有按月的账期不单独显示），没有 5 小时/每周窗口。M 图标（小米橙）的外环显示**年度额度**剩余比例；环上的象牙色刻度是“按时间平均使用时应剩的位置”，外环短于刻度说明用得偏快。任务面板顶部显示套餐剩余；右键“额度详情”列出年度额度（附“时间已过 x%”）、剩余 Credits / 已用 Credits / 今日已用 / 日均消耗、到期日、本机今日各来源用量，并按“整期平均”和“最近 7 天本机日均”中较快者预测是否会提前用完。

MiMo 面板顶部将套餐额度、今日用量和 TPS 并排显示。今日用量以 Credits 计，按本地自然日统计，自动使用 K（千）、M（百万）、B（十亿）单位；数值取本机账本与控制台当天首次读取后增量的较大值，不重复相加。首次读取前的其他设备消耗无法追溯。换日后重新计数，缓存同时保存日期，避免把昨天的总量显示成今天。

数据来源（按优先级）：

1. **控制台（精确，包含所有设备）**：只读 `platform.xiaomimimo.com` 套餐页使用的 `GET /api/v1/tokenPlan/usage` 与 `/api/v1/tokenPlan/detail`。打开设置 → **来源**，在 MiMo 一行下方点“登录小米 MiMo”（或右键 M 图标选“登录小米 MiMo…”，或运行 `CodexQuotaTray.exe --mimo-login`），会在应用自有的 Edge/Chrome 配置 `mimo-edge-profile` 中打开控制台窗口；登录后程序在控制台页面内读取套餐（与网页本身的登录方式无关），成功后窗口自动关闭并弹出结果。程序只保留 `platform.xiaomimimo.com` 的 cookie 和控制台接口请求自带的令牌请求头（如有），用 Windows DPAPI（当前用户）加密为 `mimo-session.bin`，不接触小米账号密码或 `account.xiaomi.com` 的 cookie；之后直接读取接口；被拒绝（过期或需要更多凭据）时用同一配置无窗口地在页面内读取一次并更新会话，仍失败才提示重新登录（6 小时内不重复尝试）。“退出 MiMo 登录”删除保存的会话。
2. **本机记录（实时，仅本机）**：把 Codex、Claude Code、MiMo Code（`mimocode.db`）中 `mimo-*` 请求的缓存命中 / 未命中输入和输出 token 按官方价格表换算为 Credits（v2.6-pro / v2.5-pro：2.5 / 300 / 600；v2.6-flash / v2.5：2 / 100 / 200；北京时间 0:00–8:00 ×0.8；未收录型号按 pro 估算并注明），记入 `mimo-ledger.json`。两次控制台读取之间，显示值 = 最近一次控制台“已用” + 本机此后增量，下次读取时校准。
3. **手动配置（不登录）**：在设置 → **来源** 的 MiMo 一行展开“手动填写套餐”，填写套餐总量（如 `49.2B`、`492亿`）、到期日期，可选已用 Credits 与套餐名，点“保存手动套餐”；等价于在 `%LOCALAPPDATA%\CodexQuotaTray\mimo.json` 写入 `{"total_credits": 49200000000, "period_end": "2027-06-01"}`，可选 `period_start`、`used_credits`、`used_at`、`plan_name`（未给 `period_start` 时按到期日前一年计算）；用量只来自本机记录。

Claude Code 通过 `ANTHROPIC_BASE_URL`（环境变量或 `~/.claude/settings.json`）指向 `xiaomimimo.com` 时没有 Claude 订阅可读，A 不会自动出现，这些会话计入 X。未显式指定来源时，X 在已登录/已配置 MiMo、Codex `config.toml` 或 Claude Code 指向 `xiaomimimo.com`、或存在 MiMo Code 数据目录时自动加入。程序不会用 Token Plan 的 API Key 发请求探测额度（套餐只允许在编程工具中使用，探测也会消耗 Credits）。

## 安装指南（新电脑部署）

按下面顺序做一次即可；每一步都可以在设置页确认结果。

### 1. 准备

- Windows 10/11 交互式桌面。
- 二选一：直接使用别人构建好的 `CodexQuotaTray.exe`（便携版，见[构建 EXE](#构建-exe)），或安装 64 位 **Python 3.12** 与 **Git** 从源码运行/构建。
- 想看哪个来源，就先装好并登录对应客户端（见下表）。没装的来源不会出错，只是保持关闭。
- 小米 MiMo 登录需要本机有 **Microsoft Edge 或 Google Chrome**（Windows 自带 Edge 即可）。

### 2. 安装与首次启动

```powershell
git clone https://github.com/ANTiqiuSama/CodexQuotaTray.git
Set-Location CodexQuotaTray
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 tray_app.py        # 直接运行
# 或构建 EXE（其他电脑请用“构建 EXE”一节的便携命令，不要带 runtime-temp）
```

启动后任务栏通知区域出现图标（可能在 `^` 隐藏区里，可拖到任务栏上）。右键任意图标打开**设置**；设置 → 常规里可打开“开机自启”。

### macOS 菜单栏版本

macOS 12+、Python 3.12：

```bash
git clone https://github.com/ANTiqiuSama/CodexQuotaTray.git
cd CodexQuotaTray
python3 -m venv .venv-macos
.venv-macos/bin/python -m pip install -r requirements-macos.txt
.venv-macos/bin/python -X utf8 mac_menu.py

# 在 Mac 上测试并构建 CodexQuotaTray.app
bash build-macos.sh
open dist-macos/CodexQuotaTray.app
```

也可从仓库的 **Actions → macOS menu bar build** 下载 Apple Silicon（arm64）或 Intel（x86_64）构建产物。工作流在 macOS runner 上生成 `.app` 并上传 ZIP；当前构建使用临时签名，未做 Apple 公证，首次打开可能需要在 macOS 安全设置中允许。该版本的实际菜单栏交互仍需 Mac 实机验收。

每个开启的来源各有一个顶部菜单栏图标，从左到右固定为 Claude Code → Codex → Gemini → Grok → MiMo。图标与 Windows 托盘共用同一套绘制逻辑（`tray_icons.py`）：左右分环同时显示 5 小时与每周额度，Gemini 三个模型池三等分，用尽时显示红色倒计时与 `d`/`h`/`m` 单位，生成中显示 TPS，颜色与角标含义也与 Windows 一致。点击图标可看各额度窗口或 Gemini 模型池、距重置时间（如 `4小时32分钟后重置`）、Codex 用尽时的购买额度余额、最近 TPS、读取状态；旧数据会写明读数时间和原因。菜单打开期间按悬停频率刷新该来源。“打开来源”对 Claude Code 和 Grok 在装有 herdr 时新建一个 herdr 工作区并启动对应 agent，同时把承载 herdr 的终端应用切到前台（没有 herdr 窗口时用“终端”打开一个）；没装 herdr 时在“终端”新窗口里直接运行对应 CLI。读取在后台进行，菜单栏启动时先显示本机缓存；数据保存在 `~/Library/Application Support/CodexQuotaTray/`。Finder 启动时会补充常见 CLI 路径，也可用 `CODEX_QUOTA_CODEX_EXE`、`CODEX_QUOTA_GROK_EXE`、`CODEX_QUOTA_GEMINI_EXE`、`CODEX_QUOTA_CLAUDE_EXE` 指定可执行文件。

macOS 版的 Codex 使用 `codex app-server`，Gemini 使用 `agy /usage`，Grok 和已订阅的 Claude Code 使用临时 `/usage` 终端。Claude 默认关闭，订阅开通后才通过 `CODEX_QUOTA_PROVIDERS=codex,grok,gemini,claude` 启用。MiMo 目前支持手动套餐文件和本机 Credits 账本：在上述数据目录的 `mimo.json` 填入 `total_credits`、`period_end` 等字段后可显示 M 图标。MiMo 控制台登录凭据依赖 Windows DPAPI，Mac 版暂不支持自动登录或同步控制台用量，也不会将 cookie 以明文保存。Mac 版使用原生点击菜单，Windows 专属的悬停气泡与折线图、设置窗口、模型智商与开机自启开关尚未移植。

### 3. 确认每个来源的开关

打开设置 → **来源**：每个来源一行，显示“当前：已开启/已关闭”以及原因（默认开启、已检测到、手动关闭、环境变量……）。切换开关后点“保存”，托盘自动重启生效。

| 图标 | 来源 | 默认状态 | 需要什么 | 如何开启 / 关闭 |
| --- | --- | --- | --- | --- |
| C | Codex | 开启 | 安装并登录 Codex | 设置 → 来源开关 |
| X | Grok CLI | 开启 | 安装并登录 Grok CLI | 同上 |
| G | Gemini / Antigravity | 开启 | 安装并登录 Antigravity，`agy` 在 PATH 中 | 同上 |
| A | Claude Code | **关闭** | Claude Pro/Max 订阅并登录 Claude Code | 开通订阅后在设置里打开 |
| M | 小米 MiMo Token Plan | 检测到才开启 | 登录控制台**或**填写手动套餐（见第 4 步） | 已登录/已配置/检测到 MiMo 用量时自动开启；也可手动打开 |

开关保存在 `%LOCALAPPDATA%\CodexQuotaTray\providers.json`（如 `{"claude": true, "grok": false}`；删除某项即恢复自动）。优先级：**设置页开关 > 环境变量 `CODEX_QUOTA_PROVIDERS` > 自动检测/默认值**。

### 4. 配置小米 MiMo（可选）

打开设置 → **来源**，MiMo 一行下方显示是否已登录、使用的浏览器和当前套餐。二选一：

- **方式一：登录控制台（推荐，含所有设备用量）**：点“登录小米 MiMo”，在弹出的 Edge/Chrome 窗口里登录 `platform.xiaomimimo.com` 并停留在控制台页面；读取成功后窗口自动关闭，生成加密的 `mimo-session.bin`。
- **方式二：手动套餐（不登录）**：填写套餐总 Credits（如 `49.2B` / `492亿`）和到期日期（`2027-06-01`），可选已用 Credits，点“保存手动套餐”，生成 `mimo.json`。用量只统计本机记录。

完成任意一种后，若“来源”里 M 仍是关闭，打开它并保存。

### 5. 可选项

- herdr：设置 → 任务（需要本机安装 herdr）。
- 任务命名 API：设置 → 任务。

### 常见问题

| 现象 | 处理 |
| --- | --- |
| 没有 `mimo-session.bin` / `mimo.json`，M 显示“未登录” | 还没登录也没手动配置：按第 4 步任选一种 |
| 点登录没有反应 | 确认装有 Edge 或 Chrome（MiMo 页状态卡会显示浏览器路径）；查看 `mimo-login.log` |
| 看不到 Claude（A） | 默认关闭；开通订阅后在设置 → 来源打开 |
| G 显示 `?` | 确认 `agy` 可在终端运行并已登录 |
| 日志里偶尔出现数据库锁定 | 客户端写入时短暂锁库，程序最多等待 1.5 秒后跳过本次读取，下次自动重试，无需处理 |

## 安装与启动

### 运行环境

- Windows 10/11 的交互式桌面，推荐 64 位 Python 3.12。
- 本机验收环境为 Windows 11、Python 3.12.10。Windows 10 尚缺本项目的实机验收记录。
- 对应来源的客户端及登录状态。可先只启用已准备好的来源。

Windows 悬停面板依赖 Win32 与通知区域；macOS 使用独立的 AppKit 菜单栏入口。Linux 上仅支持部分纯逻辑模块。

### 从源码运行

```powershell
git clone https://github.com/ANTiqiuSama/CodexQuotaTray.git
Set-Location CodexQuotaTray
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 固定显示来源，也适合只使用 Claude 免费桌面端的情况。
$env:CODEX_QUOTA_PROVIDERS = 'codex,grok,gemini'
.\.venv\Scripts\python.exe -X utf8 tray_app.py
```

仓库提供源码与构建入口。自行构建的程序位于指定输出目录，可直接运行 `CodexQuotaTray.exe`。正常托盘实例使用单实例锁，重复启动不会增加一套图标。

### 日常操作

| 操作或状态 | 行为 |
| --- | --- |
| 鼠标在图标上短暂停留 | 打开任务气泡面板（额度、TPS、agent 气泡与消耗预测；定时任务不显示在面板中） |
| 拖动气泡叠到另一个气泡上松开 | 标记为同一任务，合并为大气泡并重新识别 |
| 右键合并后的大气泡 | 拆回独立气泡 |
| 移开鼠标 | 关闭详情，图标恢复最新数值 |
| 左键点击图标 | 打开对应桌面程序或 CLI；Claude Code 和 Grok 在装有 [herdr](https://github.com/herdrdev/herdr) 时改为打开 herdr：把已有的 herdr 窗口恢复并切到前台（最小化时也会还原），新建并切换到一个名为 `claude` / `grok` 的工作区（space），在其中启动对应 agent（名字被占用时依次用 `-2`、`-3`…；没有打开 herdr 窗口时先用 Windows Terminal 打开一个）。启动完成前重复点击不会再多开 agent；herdr 只是没能在 30 秒内确认 agent 就绪时不弹错误框（agent 已在新工作区里启动）；没装 herdr 时仍直接打开对应 CLI |
| 右键图标 / 点击面板右下角齿轮 | 收起悬停面板并打开设置页 |
| 设置 → 概览 | 按托盘顺序排列的来源卡片，写法与悬停面板一致：每行标题是距重置的时间、旁边注明窗口或模型池，购买额度、已用完等状态同色显示；卡片底部是 TPS 与下次读取的时间和原因（悬停 / 使用中 / 空闲）。有托盘图标收在 `^` 隐藏区时顶部提示并可直接打开任务栏设置；下方为按来源堆叠的输出柱状图：右上角可切换 30 分钟 / 2 小时 / 6 小时 / 24 小时 / 7 天（记住上次选择），每根柱对齐整点时间（1 / 5 / 15 / 30 分钟，7 天为 6 小时、取自本机每小时汇总），横轴标注时钟时间、末端为“现在”，悬停某根柱显示时段与各来源 tokens；检测到高速输出（最近 3 分钟内某个整分钟达到 20 tok/s）时自动拉近到刚好容纳这段输出的更短时段（10 分钟 15 秒一柱，或 30 分钟 / 2 小时 / 6 小时），并叠加一条按约 1 分钟平均的速率折线显示升降斜率，标题旁注明“已自动拉近”，点“看 2 小时”或再点当前所选时段即退回，本段输出内不再自动拉近；卡片固定宽度、圆角，像 Windows 设置的卡片一样按窗口宽度一行排 3 块、4 块直到全部一行，换行时各卡片滑到新位置；输出图与卡片区同宽。点击卡片在窗口内滑入该来源的详情页（额度、预测、TPS、重置、用量洞察与刷新计划，以及只含该来源的输出图）；“用量”块最前面可能有一条“周额度”建议：Grok 的周额度、Codex 和 Claude Code 的每周额度在最近 48 小时内刚重置，且上周结束时还剩至少 20% 没用完，就写出重置时间和剩余比例（如“11:02 刚重置；上周还剩 38% 没用完，本周可多用”），有上周本机输出记录时再按上周用量折算这部分大约相当于多少输出 tokens；上周已用到 80% 以上、重置已超过 48 小时、上周最后一个读数离重置超过 12 小时（结尾用量不确定）或 5 小时额度的重置，都不给这条建议。其余内容：“用量”块按本机保存的记录给出今天的输出与回复数、比昨天同一时刻的增减、近 7 天日均与有输出的天数、最忙的两小时，以及近 7 天每 1% 额度约对应多少输出 tokens，点“‹ 概览”或按 Esc 返回 |
| 设置窗口的动效 | 切换页面时内容上浮淡入、进入详情页左右滑动，侧栏选中条滑动，开关、按钮、卡片悬停与分段选择器均为缓动过渡，滚轮平滑滚动并带可拖动的细滚动条；MiMo“手动填写套餐”展开收起有高度动画；面板、按钮、输入框、徽标与侧栏选中项统一为抗锯齿圆角。只排版当前页面，拖动窗口边缘时内容最多每 60 ms 重排一次；Windows“动画效果”关闭时所有过渡改为立即完成 |
| 设置 → 来源 | 每个来源（A/C/G/X/M）的开关与当前状态、原因；MiMo 一行下方直接登录 / 退出控制台或展开填写手动套餐（点击即生效）；各读取器使用的本机记录 |
| 设置 → 模型智商 | 面板横条的 AI Stupid Level Key、立即读取与要显示的模型；Grok 智商栏（LiveBench 百分比、对比模型、榜单版本）与立即读取 |
| 设置 → 任务 | herdr 适配开关与状态、当前任务列表；任务命名 API（启用、地址、Key、模型、测试连接） |
| 设置 → 常规 | 开机自启；各来源静默 / 使用中 / 光标停留的读取间隔与下次读取时间；诊断信息、全局重置预测、全部 AI 速率、数据文件夹；退出全部 |

开关和输入框的改动先暂存：窗口底部列出尚未保存的项目（如“未保存：Grok 开关、任务命名（保存后托盘会重启）”），“保存”在有改动时才可点；关闭窗口时若有未保存的改动会先询问。登录、立即读取、保存手动套餐等按钮点击即生效。

空闲时沿用深色圆盘、来源字母和额度环；环表示剩余最少的窗口，悬停可查看各窗口。生成时为来源主色的实心圆盘加 TPS 数字；额度耗尽且重置时间已知时为淡来源色空环加红色的剩余小时数，向上取整，两种状态以底色和数字颜色区分。未知值显示 `?`。角标只有三种：右上角琥珀色圆点表示旧数据（最近一次读取失败，仍显示上次读数；悬停面板底部写明读数时间和原因，例如官方额度服务出错或读取超时），左上角天蓝色圆点表示 Codex 全局重置可能性高，右上角 `d`/`h`/`m` 是耗尽倒计时的单位。每个来源固定一种色相：越容易耗尽的窗口颜色越浅（Claude 5 小时浅桃、每周陶土；Codex 薄荷绿、每周略暗；Gemini 长春花紫按 Claude/GPT → Argon → Gemini 逐级略暗），低额度只把面板数字变为琥珀（< 50%）或红色（< 20%），环和额度条颜色不变。字母和数字优先使用 Sitka Small Bold，并回退到 Cambria、Palatino、Georgia 等字体。

面板对已用完窗口显示“等待重置”及恢复时间；Codex 接口明确返回有购买额度时，用完那一列的“0%”直接换成购买额度余额（如 `59.8K`，无限为 `∞`），额度条与状态改为金色，下方注明“购买额度”。Claude 的额外额度没有直接余额信息：仅在额度耗尽后仍有可核验输出时显示“可能使用额外额度”，工作状态本身不会被当成付费使用证明。Gemini 仅高亮正在工作的模型池。

悬停期间暂缓重绘对应托盘图标，面板内容继续更新，移开后再补上图标数值。面板无法显示时回退为原生文字提示。

## 指标如何计算

### 生成 TPS

**Grok** 直接采用客户端提供的原生 TPS 数值。

**Codex** 从本地日志复算最近完成请求的生成速率：

```text
生成 TPS = 真实输出 token 数 / 首个输出项开始至最后一个输出项完成的流式时间
```

结束时间取客户端日志中最后一个输出项完成的时刻；缺少该日志时退回计数记录的写入时间。少于 16 个 token 或不足 0.3 秒的回复不计算。输出计数可包含思考及工具调用输出。它按完成时取得的计数结算，不使用字符长度、近似分词器或请求整体耗时代替。

所有带流式时长的来源（Codex、MiMo、Gemini），显示值是最近 90 秒内同一来源各次回复按 token 加权的速率（输出 token 总数 ÷ 流式时间总和），避免短小的工具调用回复让数字大幅跳动；只有一次回复时即为该次的值。该值属于最近完成的样本，不是逐 token 更新的服务端瞬时速度。样本超过 5 分钟后失效；缺少完整样本时保留额度显示。一次生成结束后，活动状态约保留 3 秒。

**Xiaomi MiMo** 按调用它的客户端分别取值，详情中注明依据：

| 客户端 | 计算方式 | 说明 |
| --- | --- | --- |
| MiMo Code | 真实输出 token 数（含思考）÷ 该消息首个输出开始至最后输出结束（思考/文本片段的 `time.start/end`，工具调用以参数流结束为止） | 不含首字延迟和工具执行时间；最接近生成 TPS。生成中的消息会点亮 M 图标 |
| Codex（模型提供方指向 MiMo） | 与 Codex 相同：真实输出 token 数 ÷ 首个输出项至计数完成的流式时间 | 客户端直连 MiMo 接口，流式时间可用 |
| Claude Code（`ANTHROPIC_BASE_URL` 指向 MiMo） | 真实输出 token 数 ÷ 请求发出（上一条用户/工具结果记录）至回复结束 | 记录中没有流式时间，只能算**端到端速率**，包含首字延迟，数值偏低；标注“含首字延迟” |

**Gemini / Antigravity** 从本地会话数据库（`~/.gemini/antigravity*/conversations/*.db`）读取每次生成的输出 token（正文 + 思考），用对应模型回复步骤（step_type 15）从创建到完成的时间计算：

```text
Gemini TPS = 输出 token 数 / 回复步骤创建至完成的时间（含首字延迟）
```

数据库被 Antigravity 锁定时读取其临时副本。

Antigravity 有多个独立额度池。程序把 `/usage` 中的桶按模型家族归类（名称含 argon → Argon；claude / gpt → Claude/GPT；gemini → Gemini），每个池取剩余最少的窗口。G 图标外圈三等分（各 120°）同时显示三个池：Argon 左上、Claude/GPT 底部、Gemini 右上，每段从自身中点向两侧填充，颜色与面板列一致；账号没有 Argon 等只剩两个池时分为左右两段；任务面板顶部并列显示各池剩余：与其他来源一样，列标题是距重置的时间，模型池名称写在额度条下方，正在使用的池会高亮。

切换账号后，在设置中点“立即刷新”或等待下一次定期读取。额度池完整替换为新返回的数据：无 Argon 时不保留该项，改由实际存在的 Opus／Gemini 参与倒计时；池或周期变化时重新积累 Gemini 消耗预测。Gemini 读取失败时显示未知，避免把旧账号额度误当成新账号。

Gemini 气泡的任务名来自会话数据库中用户消息步骤（step_type 14）的文字，桌面端还会读取 `~/.gemini/antigravity/brain/<会话>/task.md` 的标题；若客户端版本的步骤编号不同，可用 `CODEX_QUOTA_AGY_USER_STEPS=14,…` 指定。`--check-gemini-tps` 的 `user_steps`、`step_columns` 只报告字数与列名，便于核对。经 Codex 桥接使用 Gemini 时，同样采用 Codex 客户端的流式统计。

**Claude Code** 用 `api-request.id` 与回复的 `requestRef` 逐请求关联，计算真实输出 token 数 ÷ API 请求记录至最后输出记录的时间。它包含首字延迟，界面标注为 API 端到端速率；工具执行与用户等待不计入下一次请求时长。同一消息的内容块合并去重，最近多个回复按 token 加权。没有匹配时间字段时保留未知，避免用整个任务耗时代替生成时间。

### 最近 60 秒输出速率

```text
60 秒输出速率 = 窗口内已报告的输出 token 总数 / 60
```

这是窗口平均吞吐，不能直接当作单个请求的生成 TPS。统计不计输入和缓存 token，也不重复加入已包含在输出总数中的思考 token。

同一 Claude Code 消息的分块记录按消息 ID 合并计数，完成、出现后续消息或静默达到阈值后结算。现有 Codex Gemini 桥接路径做一对一去重。未知模型可能归入“其他模型”，不保证每种第三方桥接记录都能自动识别。

TPS 观察器每秒读取已有本地信号，本身不发送模型生成请求。额度查询会启动对应 CLI 或 App Server，并由客户端处理网络访问和登录状态。

## 刷新与缓存

额度查询由 `refresh_scheduler.py` 调度，TPS 观察独立运行。每个来源分三档：光标停留（悬停）最快，正在使用其次，静默最慢；各来源独立读取，慢的（Grok 每次要开 CLI 会话）不会拖住其他来源。两次读取之间至少间隔上一次读取用时的 2 倍，避免读取本身很慢时接连启动。

| 触发条件 | Codex | Gemini | Grok | Claude Code |
| --- | --- | --- | --- | --- |
| 启动、手动刷新、检测到唤醒时钟跳变 | 尽快刷新 | 尽快刷新 | 尽快刷新 | 尽快刷新 |
| 光标停在图标或任务面板上（面板里画出曲线的正在使用的来源同样按悬停刷新） | 每次悬停都立即读取一次（正在读取时，读完再补读一次；移开后 3 秒内又回来算同一次悬停），之后每 5 秒 | 同左，之后每 10 秒 | 同左，之后每 30 秒 | 同左，之后每 5 秒；每次新的悬停都会用 `/usage` 核对（状态栏读数在 45 秒内则直接采用），同一次悬停内最多每 30 秒核对一次 |
| 正在使用（正在生成，或 herdr / 本机会话中有活跃 agent） | 30 秒 | 45 秒 | 2 分钟 | 20 秒 |
| 静默 | 2 分钟 | 2 分钟 | 10 分钟 | 1 分钟 |
| 一次生成结束 | 5 秒后 | 8 秒后 | 20 秒后 | 20 秒后 |
| 额度用尽，距重置较远 | 照常（可能提前重置） | 照常 | 最长 3 小时一次 | 照常 |
| 额度用尽，重置前 30 分钟内 | 每 3 分钟 | 每 3 分钟 | 每 3 分钟 | 每 3 分钟 |
| 额度用尽，重置前 5 分钟至重置后 10 分钟 | 每 1 分钟 | 每 1 分钟 | 每 1 分钟 | 每 1 分钟 |
| 已过重置时间但仍显示用尽（服务端或 CLI 滞后） | 1 小时内每 3 分钟，之后按空闲间隔 | 同左 | 同左 | 同左 |
| 额度用尽但重置时间未知或不可信 | 照常 | 照常 | 按静默间隔（10 分钟），不再长时间等待 | 照常 |
| 额度用尽时光标停在图标上 | 按悬停间隔 | 同左 | 同左 | 同左 |
| 读取失败 | 30 秒、1 分钟、2 分钟、5 分钟退避，不超过静默间隔 | 同左 | 同左 | 连续失败 3 次后每 12 小时 |
| 账号没有该额度（如无 Claude 订阅） | — | — | — | 每 12 小时，且不再因使用而刷新 |
| 已知额度重置时间 | 重置后约 5 秒 | 同左 | 同左 | 同左 |

MiMo 静默 5 分钟、使用中 1 分钟、悬停 10 秒（控制台读取是一次 HTTPS 请求；本机记录随时折算用量）。Claude Code 的额度由状态栏写入本机文件，读取开销很小，所以间隔最短；但状态栏只在 CLI 会话刷新时写入，Claude App 或网页上的用量不会经过它，所以悬停时状态栏读数一旦超过 45 秒，就用隐藏的 `/usage` 会话（约 7 秒）核对，取两者中较新的读数。每次读取之后至少间隔该次耗时的 2 倍再读下一次。

Claude Code 的状态栏数值来自各会话自己最后一次请求，每次重绘都会以新的时间戳写回，所以“最新写入”常常是空闲会话里的旧数值。程序把所有会话的读数合在一起：较新的重置窗口优先，同一窗口内取已用最多的（窗口内用量只增不减）；Claude 正在使用时每 3 分钟用 `/usage` 核对一次，`/usage` 与状态栏之间取更靠后的那个，两次核对之间也不会被落后的状态栏读数拉回。同一窗口里明显回升的读数先搁置并立即用 `/usage` 核对；搁置不算读取失败，不会进入连续失败后的 12 小时暂停。1 秒一次的本机快速同步只用状态栏，从不在其中运行约 7 秒的 `/usage`。

CLI 显示的重置时间不带年份（如 `Next reset: September 28, 11:53`）。刚过去几天内的日期按“刚发生的重置”处理，不再被误判为明年。

手动“立即刷新”总是立即读取。未订阅 Claude 时也可以用 `CODEX_QUOTA_PROVIDERS` 去掉 Claude 图标。

Codex、Grok、Claude 的额度窗口恢复（剩余回升至少 10 个百分点）时会弹出 Windows 通知，例如“Grok 额度已重置：每周窗口 0% → 100%”；早于原定重置时间时注明“提前重置”。可用 `CODEX_QUOTA_RESET_NOTIFY` 调整来源列表。

Codex 每次响应后会把服务端返回的额度写入本地会话记录（`token_count` 事件中的 `rate_limits`）。托盘每秒读取这些记录，比上次 App Server 读取更新时立即更新 C 图标，无需等到下一次定时刷新；会话记录出现“usage limit / rate limit”错误时，约 3 秒后立即重新读取 App Server。

调度按来源管理，同一轮到期的读取并行执行。CLI 启动、网络和已有读取任务会影响实际完成时间。右键“额度详情”显示下一次计划刷新。

Grok 的 `/usage` 在每次新建的独立进程中读取，包含隐藏终端初始化的总超时为 35 秒；超时只结束该临时进程及其子进程，保留上次有效额度并退避重试，避免阻塞其他来源。`--check-grok work/checks/grok.json --raw` 还会保存 `grok.raw.json`，仅含 CLI 的 `Weekly limit` 和 `Next reset` 原文，方便核对日期与时区。

多数来源查询失败时继续显示最后有效数值，同时保留原更新时间并标记“旧数据”；Gemini 为适应账号切换，失败时显示未知。`performance.json` 只在内容变化或距上次保存达到 60 秒时写入；文件时间没有逐秒变化，并不直接意味着观察器停止。

## 额度耗尽预测

每次成功刷新后，程序记录时间、已用百分比与重置时间。它优先选择当前重置窗口内最近 3 小时的样本；近期样本不足时回退到该窗口的完整历史。至少需要两个读数，且时间跨度达到 10 分钟。

程序对已用百分比与时间做最小二乘直线拟合，估计每小时消耗的额度百分点：

```text
预计耗尽所需秒数 = 剩余百分比 / 每小时消耗百分点 × 3600
```

例如，若示例账号一小时内从已用 20% 升到 30%，线性估计为每小时消耗 10 个百分点。剩余 70% 对应约 7 小时。这只是说明计算方式的假设数据。

当预计耗尽时间早于重置，右键“额度详情”提示约多久后用完；足以覆盖到重置时提示预计剩余量。额度回升或重置窗口变化时，旧窗口样本被截断或排除。历史按窗口最多保留 400 个样本，清理超过 8 天的记录。

预测依赖近期使用节奏、离散的百分比读数和客户端提供的窗口信息。样本不足时不生成耗尽估计；它不能预见后续使用强度或服务端规则变化。

## Codex 全局重置预测

OpenAI 的 Codex 团队有时会提前重置所有用户的 Codex 用量（通常由 @thsottiaux 在 X 上宣布）。社区追踪站点会据此给出未来 24 / 48 小时内发生全局重置的概率。托盘按顺序读取以下公开、无需密钥的接口，取第一个可用的结果：

| 顺序 | 来源 | 接口 |
| --- | --- | --- |
| 1 | willreset.com | `GET /api/notify`（约 0.3 KB，官方文档列出的提醒用接口） |
| 2 | codex-reset.com | `GET /api/forecast` |
| 3 | willcodexreset.com | `GET /api/reset-radar`（约 1 MB，仅作后备） |

- 每 30 分钟读取一次；概率较高时改为每 10 分钟。结果缓存在 `codex_reset.json`。
- 24 小时概率达到阈值（默认 60%）时：C 图标左上角出现天蓝色圆点，悬停提示首行与任务面板顶部显示柔和钢蓝色提示条（有官方预告时带“官方预告”标签），并弹出一次系统通知；Codex 剩余不足 30% 时，提示中会建议等重置后再大量使用。
- 社区确认新的全局重置后，弹出通知并立即重新读取 Codex 额度。
- 右键“额度详情”显示来源、24 / 48 小时概率、上次全局重置时间和 @thsottiaux 最新信号；右键 C 图标的“全局重置预测”可打开来源网站。

这些是社区估计，不是 OpenAI 的官方时间表。请求只发送普通 HTTP GET，不包含任何账户信息。

## 模型智商（AI Stupid Level）

[AI Stupid Level](https://aistupidlevel.info/) 持续给主流模型跑基准测试，托盘只读取它的编程榜（`sortBy=coding`），显示各模型近 24 小时的编程分（0–100，越高越好）。面板顶部为两个分开的圆角标签：左侧为简短的 `Codex 重置 NN%`，右侧显示“智商”与三个模型的编程分（如 `GPT-6.1 Sol 83  GPT-6 Luna 86  Opus 5.5 93`）。

- 需要免费 API Key：设置 → 模型智商 → “获取免费 Key”，填入 `asl_live_…` 后保存或点“立即读取”。也可用环境变量 `CODEX_QUOTA_IQ_KEY`（优先于设置页）。Key 仅保存在本机 `model_iq_config.json`。
- 免费 Key 每天 10 次（00:00 UTC 重置）：程序每 3 小时自动读取一次、每天最多 7 次，其余留给手动“立即读取”；调用次数记在 `model_iq.json`，重启不会多用；Key 无效时同样等满 3 小时再试；遇到 429 当天停止。
- 面板始终显示上一次成功查询的结果，不显示“今日已用完”或读取失败等提示（这些只在设置页说明）；接口返回空数据也不会清掉已有结果。从旧版升级时，旧的综合分缓存继续显示，直到下一次编程榜读取成功后替换；当天已遇到 429 则等次日额度。
- 该来源正在运行的模型（显示 TPS 的那个，用来源颜色标出）始终排在最前，其余位置默认为编程分最高的模型（每次读取后自动更新）；在设置页的完整列表中勾选最多 3 个模型可自定义，按勾选顺序显示，“恢复默认”清空选择。点击面板顶部横条直接打开该设置页。
- 按该站条款，展示数据时须注明来源 aistupidlevel.info；设置页底部有来源链接。

### Grok 智商栏

AI Stupid Level 不收录 xAI 的模型，所以 Grok 面板中间空出的那一栏改用 [LiveBench](https://livebench.ai/) 最新的公开榜单：计算每个模型的加权总分（LiveBench 首页的 Overall），以榜上最高分的模型为 100%，显示 Grok 4.7 达到它的百分之几，下方写明对比的两个模型，如 `Grok 4.7/Fable 5.1`。已发布的榜单不会再变，程序每 3 小时检查一次首页，只在出现新版本时重新下载；读取失败时保留上一次结果。

## Agent 任务气泡

悬停任一额度图标都会打开任务面板（旧的详情卡片已移除）；没有在工作的 agent 时也照常显示。未开通订阅的 Claude 没有额度可显示，仍用文字提示。指针可以移入面板操作，移出约半秒后收起；面板不抢键盘焦点。

| 区域 | 内容 |
| --- | --- |
| 顶部 | 5 小时、每周剩余额度与最近 TPS，打开时数字与额度条从零滚动到当前值；右上角为状态（N 个在工作 / 空闲 / 旧数据 / 读取失败），工作时标题圆点有声呐波纹；MiMo 并列显示套餐剩余、今日 Credits 与 TPS；agent 仍在工作但长回复或工具调用暂未产生新样本时，保留 30 分钟内最近一次测得的 TPS，不再跳成 0；上方横条显示 Codex 社区重置概率与所选模型智商，点击打开“模型智商”设置页 |
| 额度区下方的重置三栏 | 仅显示来源全名和倒计时，按时间由近到远排列。Codex 等来源选择当前限制可用额度的窗口；周额度耗尽时不展示无助于恢复使用的 5 小时重置，两个窗口均耗尽时等较晚的窗口恢复。Gemini 的 Argon 和 Opus 独立参与排序，可占两栏，并注明池名（Opus 显示为“Gemini · Opus”以免与 Claude 混淆，超过一天只显示天数）；Opus 对应控制台的 Claude/GPT 共享池时间。MiMo 套餐到期不算周期重置 |
| 气泡内 | 任务名：最近一次实质请求（“继续”“好的”之类不覆盖任务），缺失时用客户端线程标题或项目文件夹 |
| 气泡下 | 当前动作：运行测试、编辑某文件、检索代码、查资料、派发子任务、思考中等；悬停气泡时展开完整任务描述 |
| 折线图下 | 一段简短提示：概括正在工作的 agent，并给出一项可选的新点子；接入 MiMo 任务命名 API 后由 MiMo 结合历史话题生成，未接入或读取失败时显示本机提示 |
| 面板底部 | 按当前速度是否能用到重置／到期的预测，以及右侧的设置齿轮图标（无底板，悬停时变色并微转） |
| 外圈 | 颜色对应模型来源；呼吸波纹表示正在生成，暗淡表示 3 分钟内刚完成；大气泡上的小圆点是各并行成员 |

所有来源的 agent 都会出现在同一面板，被悬停来源的气泡高亮；各图标的面板大小与气泡排列一致，切换图标时不跳动（无 TPS 时显示 0 tok/s，Codex 全局重置提示条在所有面板显示）。气泡分组规则：

- **自动**：客户端自己派生的子 agent 归入父任务——Codex `spawn_agent` 子线程（`parent_thread_id`）、Claude Code `subagents/`、MiMo Code 子会话（`parent_id`）。
- **同一进程的附属线程**：客户端为自己开的旁路线程（只有流式日志、没有请求和文件夹的会话，如标题生成、审批复核、压缩；或标着 subagent 却缺少父线程的会话）并入同一客户端、同一文件夹最近活跃的主会话，不再单独冒出小气泡。
- **herdr 窗格**：使用 [herdr](https://github.com/herdrdev/herdr) 管理 agent 时，同一窗格（pane）里的所有会话合并为一个气泡；herdr 报告的“等待确认”（blocked）显示为“等你确认”，没有本地日志的 agent（opencode、Cursor 等）也会按窗格显示。详见下方 [herdr 适配](#herdr-适配)。
- **手动**：拖动气泡叠到另一个上，目标亮起光环、被拖的气泡被轻轻吸过去，松开即合并（面板上不再显示操作说明）；分组按会话 ID 保存，重启后仍生效。右键大气泡拆开，拆开后也不再自动归组。
- **重新识别**：合并后先按本地规则命名（有父任务时用父任务的请求，否则选与其他成员最相近的请求），气泡下显示“N 路并行 · 各自分工”。

### herdr 适配

装好 herdr 并运行其服务后无需配置：程序每 2.5 秒执行一次 `herdr agent list`（无窗口子进程，与其子进程一起在作业对象中结束）；herdr 没有 agent 窗格时改为每 10 秒，服务未运行或读取失败时每 30 秒，找不到时每分钟重新查找一次 `PATH`。

- 每个 herdr 窗格对应一个 agent 进程。窗格报告的原生会话 ID（`agent_session`）与本地 Codex 线程、Claude 会话等对应，同窗格的会话归为一个气泡；未安装 herdr 集成插件时，同客户端、同文件夹且唯一的窗格也会对上。建议执行 `herdr integration install codex`、`herdr integration install claude` 等，让 herdr 报告会话 ID。
- 没有会话 ID 时，agy（Antigravity CLI）等不记录文件夹的客户端：只有一个工作中窗格且只有一个进行中的对话时直接对上，气泡带回该对话的 TPS；herdr 显示工作中的窗格也计入该来源的“在工作”，图标切到 TPS。
- 窗格状态：working → 工作中，blocked → 等你确认（气泡保持亮起），done → 刚完成；idle 且没有本地日志的窗格不显示。
- 只调用 herdr 的只读接口，不读写其窗格内容。herdr 未安装或服务未运行时一切照旧，诊断中的 `herdr` 项会显示“未发现 / 服务未运行 / 已接入”。
- 环境变量：`CODEX_QUOTA_HERDR=0` 关闭；`CODEX_QUOTA_HERDR_EXE` 指定 `herdr.exe` 路径；`CODEX_QUOTA_HERDR_SESSION` 读取指定名称的 herdr 会话。

### 定时任务

定时任务（Codex 自动化、Claude Code 计划任务等）不进入气泡，也不在悬停面板显示。面板底部保留额度消耗预测；定时任务识别结果仍写入诊断，便于排查被排除的会话。

额度预测通常用当前窗口较长时间的消耗斜率；当最近 20 分钟内至少三次读数、跨度达到 1 分钟且已用额度持续增加至少 3 个百分点时，会提前用这段较快速度做条件预测。短时速度可能在任务结束后下降，因此文案标为“按近期较快用量估算”；预计重置时只剩 5% 及以下也会提示接近用尽。预测不改变官方额度或重置时间。
折线图中的实线是已读取的额度，虚线是从最新读数按同一预测速度延伸的趋势；右侧只为虚线留出较窄的预测区，超出图表范围的用尽时间仍写在文字预测中。

识别依据（尽力而为，客户端版本不同可能需要校准）：Codex 的 `~/.codex/sqlite/codex-dev.db`、状态库中的自动化表与 `automations/*/automation.toml` 定义（不把 `memory.md` 当作任务），以及 Claude `~/.claude/scheduled-tasks/*/SKILL.md`；会话元数据中的 automation / schedule / cron / heartbeat / 定时 标记，以及用户消息中的 `<heartbeat><automation_id>…` 信封。heartbeat 只标记该次运行，不会把同一聊天后来手动发起的任务永久归为定时任务。7 天内间隔超过 30 分钟的重复请求也会被识别；通用占位标题不计入。`--check-agents` 输出中的 `schedules`、`schedule_chips` 与各 agent 的 `origin`、`schedule` 字段可用于核对。

### 可选：轻量模型命名

小米 MiMo / Token Plan 可使用控制台对应的 Base URL 和 `mimo-v2.6-flash`。程序会自动使用 `api-key` 认证、关闭深度思考并限制命名输出长度；密钥只保存在本机配置或环境变量中。气泡的当前动作始终优先显示本机最新工具状态，不会被较早的模型摘要覆盖。

默认完全本地，不发送任何内容。若希望标题更精炼，可配置任意 OpenAI 兼容的 Chat Completions 接口（如 MiMo、DeepSeek、本地 Ollama 等的小模型）：

```powershell
$env:CODEX_QUOTA_SUMMARY_URL = 'https://api.example.com/v1'   # 或完整的 /chat/completions 地址
$env:CODEX_QUOTA_SUMMARY_KEY = 'sk-...'
$env:CODEX_QUOTA_SUMMARY_MODEL = 'small-model-name'
```

也可以在设置 → 任务的“任务命名”中配置、测试并保存，或写入 `%LOCALAPPDATA%\CodexQuotaTray\agent_summary.json`：`{"url": "...", "key": "...", "model": "...", "enabled": true}`；设置 `enabled: false` 保留配置并停用。环境变量优先于文件。启用后，每个气泡在其请求变化时才发起一次后台请求（结果按输入缓存，失败后 5 分钟内不再请求），合并的大气泡会请求一个总任务名和每个成员的分工；识别期间气泡外圈显示旋转弧线。发送的内容仅为每个成员最近一次请求的前 400 字、项目文件夹名和当前动作。
当该配置指向小米 MiMo 时，所有来源图标的悬停面板共用同一段进展与建议：先概括当前工作，再跨任务发散给出一项可选的新点子。每次发送最多 5 个正在工作 agent 的短标题与动作、来源剩余额度的 5% 档位、检测到的 AI 客户端名称，以及从各来源近 30 天本机历史中随机抽取的最多 18 条短任务话题。历史话题来自任务标题或首条请求的简短提炼，不发送整段对话、回复、文件路径或密钥。提示在内存中缓存约 2 分钟，失败后 5 分钟退避，MiMo 不可用时自动回到本机提示；这些提示不会自行启动或停止任务。

## 配置

### 选择来源

推荐在设置 → **来源** 中用开关选择（保存到 `providers.json`，优先于环境变量）。也可以用环境变量：

只对当前 PowerShell 会话设置：

```powershell
$env:CODEX_QUOTA_PROVIDERS = 'codex,grok,gemini'
```

保存为当前 Windows 用户的设置：

```powershell
[Environment]::SetEnvironmentVariable('CODEX_QUOTA_PROVIDERS', 'codex,grok,gemini', 'User')
```

环境变量在程序启动时读取。退出旧托盘后，从包含新变量的终端或登录环境重新启动。恢复自动检测时，清除 `CODEX_QUOTA_PROVIDERS`；未设置该变量时，默认 C/X/G，并按检测结果加入 MiMo；Claude 等待手动启用。设置页保存过的开关始终优先。

### 图标固定顺序

托盘图标自左向右固定为 **Claude Code → Codex（GPT） → Gemini → Grok → MiMo**（未启用的来源跳过，其余保持相对顺序）。位置不随剩余额度、重置时间或是否用完而变化。Gemini 图标是一整圈三等分：左上 Argon、下方 Claude/GPT（5 小时）、右上 Gemini，各 120°。

Windows 没有移动通知区域图标的接口，而且删除后重新添加的图标会被资源管理器放回它记住的位置，因此程序**不移动图标**：程序读取各托盘图标在屏幕上的实际位置，再按上述固定顺序把来源分配给这些图标（图标图像、悬停面板、右键菜单、左键动作一起切换）。所以无论系统的插入方向如何、手动拖动过与否，都按屏幕上从左到右的顺序显示；程序每 2 秒检查一次，拖动图标后几秒内即按顺序重新分配；鼠标停在图标上或任务面板打开时不校正。

五个托盘图标各用一个独立的图标编号（0–4）。Windows 按“程序 + 编号”分别记住每个图标在任务栏还是在 `^` 隐藏区、以及它的位置；以前五个图标共用编号 0 时，Windows 把它们当作同一个图标，会随机把其中一个收进隐藏区。程序只读取（不修改）Windows 的这份记录：分配顺序时只算显示在任务栏上的图标，被收起的图标分给排在最后的来源，所以任务栏上看得到的部分始终是上面的顺序。日志中出现 `tray icons are not on the taskbar` 时，到“设置 → 个性化 → 任务栏 → 其他系统托盘图标”打开全部 CodexQuotaTray 图标，或从 `^` 中拖到任务栏上；想让五个图标相邻、不被其他程序的图标隔开，也需要手动拖一次。

### 配置项对照

| 变量 | 用途 |
| --- | --- |
| `CODEX_QUOTA_PROVIDERS` | 显示的来源键，支持 `codex,grok,gemini,claude,mimo`；按注册表顺序显示 |
| `CODEX_QUOTA_CODEX_EXE` | 用于额度查询的 `codex` App Server 可执行文件路径 |
| `CODEX_QUOTA_RESET_NOTIFY` | 额度重置时弹出 Windows 通知的来源，默认 `codex,grok,claude`；设为 `none` 关闭 |
| `CODEX_QUOTA_RESET_WATCH` | 设为 `0` 关闭 Codex 全局重置预测（不再访问社区站点） |
| `CODEX_QUOTA_IQ_KEY` | AI Stupid Level API Key（优先于设置页；不设置且设置页为空时不读取模型智商） |
| `CODEX_QUOTA_RESET_ALERT` | 醒目提示的 24 小时概率阈值，默认 `60`（百分比） |
| `CODEX_QUOTA_GROK_EXE` | 用于额度查询的 Grok CLI 路径 |
| `CODEX_QUOTA_GEMINI_EXE` | 提供对应 `/usage` JSON 的 `agy` 可执行文件路径 |
| `CODEX_QUOTA_CLAUDE_EXE` | Claude Code 的 `.exe` 或 npm `.cmd` 入口路径 |
| `CODEX_HOME` | Codex 本地日志与会话数据根目录 |
| `GROK_HOME` | Grok 本地记录根目录 |
| `CLAUDE_CONFIG_DIR` | Claude Code 数据根目录，默认 `~/.claude` |
| `CODEX_QUOTA_MIMO_BROWSER` | MiMo 登录窗口使用的 Edge/Chrome 路径 |
| `CODEX_QUOTA_MIMO_PLATFORM` | MiMo 控制台地址，默认 `https://platform.xiaomimimo.com`（测试用） |
| `CODEX_QUOTA_SUMMARY_URL` / `_KEY` / `_MODEL` | 可选的 OpenAI 兼容接口，用于把 agent 请求归纳为气泡标题；未设置时只用本地规则 |
| `CODEX_QUOTA_HERDR` / `_HERDR_EXE` / `_HERDR_SESSION` | herdr 适配：设 `0` 关闭、指定可执行文件、指定 herdr 会话名 |
| `CODEX_QUOTA_LEGACY_DPI=1` | 关闭悬停线程的 Per-Monitor V2 模式，供旧显示环境排错 |

路径应指向实际安装位置，例如：

```powershell
$env:CODEX_QUOTA_CLAUDE_EXE = 'C:\Tools\ClaudeCode\claude.cmd'
$env:CODEX_QUOTA_PROVIDERS = 'codex,grok,gemini,claude'
```

读取器路径覆盖与左键启动入口是两项设置。左键启动方式由 `providers.py` 中的 `ProviderSpec.launch` 决定；安装方式不同时，需要调整对应入口。

Claude 原生实时额度使用 `claude-status/` 中的数值记录；`claude-probe/` 仅供未接入 status line 的旧版 `/usage` 兼容读取。

## 本地数据与隐私

程序自身的数据目录为 `%LOCALAPPDATA%\CodexQuotaTray`：

| 文件或目录 | 内容 |
| --- | --- |
| `state.json` | 最后一次额度读数、时间、旧数据标记和错误状态 |
| `performance.json` | 数值化输出计数、速率、来源及可观测状态 |
| `history.json` | 预测用的时间、已用百分比、重置时间 |
| `usage.json.gz` | 输出记录：最近约 26 小时逐条保存（来源、会话 ID、模型名、完成时间、token 数，不含提示词或回复内容），更早的按来源每小时汇总保留 90 天；最多每分钟写一次、退出时再写一次，通常几十 KB。启动时先放回最近一天的记录，输出图与悬停折线不再因重启清空，重新读到的同一条记录按 ID 跳过，MiMo 账本不会重复计入 |
| `codex_reset.json` | 最近一次 Codex 全局重置预测 |
| `grok_iq.json` | 最近一次 LiveBench 榜单版本、Grok 4.7 与最高分模型的总分 |
| `model_iq.json`、`model_iq_config.json` | 最近一次模型智商读数与当天调用次数；AI Stupid Level API Key 与悬停面板所选模型 |
| `app.log` | 运行日志（错误与每次额度刷新结果），达到约 1 MB 后轮转，保留两份备份 |
| `diagnostics.txt` | 从菜单生成的诊断信息 |
| `claude-probe/` | Claude Code 临时额度探测的工作目录 |
| `mimo-session.bin` | MiMo 控制台 cookie（仅 platform 域，DPAPI 加密） |
| `mimo-edge-profile/` | MiMo 登录用的应用自有浏览器配置 |
| `mimo-ledger.json`、`mimo-days.json` | 本机 MiMo Credits 账本（40 天）与每日首次控制台读数 |
| `mimo.json` | 可选的 MiMo 手动套餐配置（设置 → 来源 → MiMo 填写） |
| `providers.json` | 设置 → 来源中保存的各来源开关（未列出的来源按自动检测） |
| `settings.json` | 设置页中的 herdr 适配与图标排序选择（对应环境变量设置时以环境变量为准） |
| `agent_groups.json` | 手动合并的任务分组与拆开记录（仅会话 ID，7 天未用自动清理） |
| `agent_summary.json` | 可选的气泡命名接口配置 |
| `mimo-login.log` | MiMo 登录/续期诊断：步骤、HTTP 状态码、cookie 与请求头的**名称**（不含值） |

TPS 读取器会在内存解析本地会话记录。任务气泡所需的请求摘要、工具名和线程标题同样只在内存中，不写入任何文件；只有配置了命名接口时，才会把上节所述的截断请求发往该接口。`performance.json` 保存数值投影、模型名等统计字段，不包含已解析的消息正文。程序不直接打开客户端的 `auth.json` 或提取 token、cookie；登录由已有客户端处理。唯一例外是 MiMo：控制台会话 cookie 由程序自己的登录窗口取得，并加密保存在上表位置。

日志与诊断可能包含本地路径、错误文本、账户用量和软件状态。`--check-claude --raw` 额外保存终端文本，仅自动遮盖邮箱，其他内容仍需在共享前检查。仓库的 `.gitignore` 排除了运行缓存、日志、构建输出和常见本地凭据文件。

## 构建 EXE

在已安装依赖的 Python 环境中运行：

```powershell
# build.ps1 使用 PATH 中的 python；虚拟环境用户先在当前终端调整 PATH。
$env:Path = (Join-Path $PWD '.venv\Scripts') + ';' + $env:Path
.\build.ps1 -DistPath .\outputs
```

脚本先运行测试，再生成单文件、无控制台的 `outputs\CodexQuotaTray.exe`。不指定 `-DistPath` 时，默认将 EXE 放在仓库父目录，沿用本机维护部署约定。

**维护脚本会将构建时仓库父目录下的 `runtime-temp` 写入 EXE，作为依赖解包位置。** 这适合固定位置的本机部署。若要把 EXE 分发到其他电脑，可使用默认系统临时目录进行构建：

```powershell
python -m PyInstaller --noconfirm --onefile --windowed `
  --collect-binaries winpty --collect-data winpty --collect-data tzdata `
  --name CodexQuotaTray --distpath outputs `
  --workpath work/pyinstaller --specpath work tray_app.py
```

替换正在使用的 EXE 前，从任意图标的菜单选择“退出全部”。Windows 字体从本机系统目录加载，不随仓库分发；打包依赖仍适用各自的许可证。

## 自检与排错

先正常运行一次程序，等待本地缓存生成，再使用诊断入口。以下命令以自行构建的 EXE 为例：

```powershell
New-Item -ItemType Directory -Path work/checks -Force | Out-Null
.\outputs\CodexQuotaTray.exe --check-tps work/checks/tps.json
.\outputs\CodexQuotaTray.exe --check-codex work/checks/codex.json
.\outputs\CodexQuotaTray.exe --check-gemini-tps work/checks/gemini-tps.json
.\outputs\CodexQuotaTray.exe --check-reset work/checks/reset.json
.\outputs\CodexQuotaTray.exe --check-grok work/checks/grok.json
.\outputs\CodexQuotaTray.exe --check-claude work/checks/claude.json
.\outputs\CodexQuotaTray.exe --check-mimo work/checks/mimo.json
.\outputs\CodexQuotaTray.exe --check-hover work/checks/hover.json
.\outputs\CodexQuotaTray.exe --check-agents work/checks/agents.json   # 另存 agents.png 面板预览
```

`--check-tps` 读取两次本地统计；`--check-reset` 读取全局重置预测，并列出各来源的错误；`--check-gemini-tps` 输出最新几个 Antigravity 数据库的结构（字段编号、数值、时间，不含提示词和回复文本）以及当前 Gemini TPS；`--check-codex` 读取 App Server 额度，加 `--raw` 时另存 `codex.raw.json`，包含 App Server 原始响应、最近一条会话记录中的 `rate_limits` 和额度用尽错误时间；`--check-grok`、`--check-claude`、`--check-mimo` 检查对应真实额度链路。每次定时刷新的结果（剩余、重置时间、是否旧数据、错误）都会写入 `app.log`，便于排查图标未更新。额度读取失败时返回非零退出码，并保留结果中的错误说明。

`--check-hover` 会短暂显示约 4 秒的置顶全屏测试窗口，验证任务面板置顶、覆盖全屏窗口、位于工作区内、持续刷新不闪烁与焦点保持，随后自动关闭。它不移动真实鼠标。

需要核对 Claude CLI 新版本的界面格式时，可显式使用：

```powershell
.\outputs\CodexQuotaTray.exe --check-claude work/checks/claude.json --raw
```

| 现象 | 检查方向 |
| --- | --- |
| 没看到图标 / 顺序缺了某个来源 | 检查 Windows 隐藏图标区域（设置 → 个性化 → 任务栏 → 其他系统托盘图标，五个 CodexQuotaTray 都要打开）、来源选择和 `app.log` |
| 只显示字母或 `?` | 查看详情中的额度状态；确认客户端入口和登录已准备好 |
| Codex 额度用完但图标没变 | 查看 `app.log` 中的 `quota codex` 与 `codex quota from session record` 行；用 `--check-codex <文件> --raw` 核对 App Server 各额度桶与会话记录 |
| Gemini 没有 TPS | 运行 `--check-gemini-tps <文件>`，查看数据库是否被找到、是否读取出错、回复步骤是否有创建/完成时间 |
| 生成时没有 TPS | 查看是否取得完整样本，以及该来源是否提供生成 TPS |
| Gemini 查询超时 | 检查 `agy` 的登录、网络和 `/usage` 输出，旧数值会保留并标记 |
| Claude 没有显示 | 默认关闭；开通订阅并登录 Claude Code 后，在设置 → 来源打开 A 开关 |
| Claude 有图标却没有额度 | 检查 Claude Code 的登录与订阅窗口；免费桌面端不通过该读取器接入 |
| M 显示“未登录”或登录窗口不关闭 | 设置 → 来源的 MiMo 一行点“登录小米 MiMo”或填写手动套餐（也可右键 M 选“登录小米 MiMo…”，或运行 `--mimo-login`）。登录后停留在控制台页面；仍不成功时查看 `mimo-login.log`；用 `--check-mimo <文件> --raw` 保存去除身份字段的接口原文以核对格式 |
| 任务面板在全屏应用中没有出现 | 先让任务栏图标可见；独占全屏和安全桌面尚不保证覆盖 |
| 中文出现方框或显示模糊 | 检查系统中文字体，必要时用 legacy DPI 选项排错 |
| 构建提示 EXE 被占用 | 退出正在运行的目标程序，或构建到另一输出目录 |

## 项目结构与扩展

```text
providers.py          来源注册表、检测、配色、刷新间隔与启动入口
quota_sources.py      额度读取、双窗口解析、缓存与失败保留
cli_usage.py          CLI 临时会话、输出解析与时间处理
tps_sources.py        本地记录发现、输出计数、去重与速率汇总
refresh_scheduler.py  按需刷新、失败退避和重置调度
forecast.py           数值历史与额度耗尽估计
usage_store.py        本机保存的输出记录（逐条一天、每小时汇总 90 天）与用量洞察
reset_watch.py        Codex 全局重置的社区预测读取与提示文案
model_iq.py           AI Stupid Level 模型智商读取、每日调用预算与面板显示选择
grok_iq.py            LiveBench 榜单读取：Grok 4.7 相对最高分模型的百分比
icon_art.py           额度与 TPS 圆盘绘制、字体缓存
detail_card.py        面板共用的字体、配色、阴影与合成
hover_details.py      Win32 分层窗口、DPI、悬停、任务面板窗口及文字提示回退
agent_tasks.py        各 agent 在做什么：请求/动作提取、自动与手动分组、可选模型命名
herdr_source.py       可选 herdr 适配：按窗格归并会话、补充无本地日志的 agent
agent_panel.py        任务气泡面板：布局、弹簧动画、拖动合并与拆分、Pillow 渲染
settings_window.py    设置窗口（Tk 深色界面：侧栏导航与五个页面、来源详情页）
ui_motion.py          设置窗口的缓动动画、节流与字符宽度缓存
settings_view.py      设置页的可视化：来源卡片、输出柱状图、任务列表（Pillow 绘制）
hover_check.py        任务面板窗口显示自检
mimo_usage.py         MiMo 控制台读取、登录、Credits 账本与年度节奏
tray_app.py           程序入口、菜单、线程协调与登录启动项
mac_menu.py           macOS 原生菜单栏入口、后台读取与菜单明细
tray_icons.py         托盘 / 菜单栏共用的图标状态：分环、倒计时、TPS 与旧数据说明
build-macos.sh        macOS 测试与 .app 构建入口
test_*.py             额度、速率、任务面板、定时任务、调度及 Claude 回归用例
demo_assets.py        生成 README 演示图（演示数据）
build.ps1             测试与 PyInstaller 构建入口
```

新增来源时，在 `providers.py` 添加 `ProviderSpec`，在 `quota_sources.py` 实现额度读取并登记到 `_reader()`。若有可核验的本地计数，再为 `tps_sources.py` 增加观察器及去重规则。为新格式增加真实结构的脱敏测试用例，缺失字段应保持未知。

## 测试与验收状态

完整测试命令：

```powershell
python -X utf8 -m unittest -v test_quota_sources.py test_tps_sources.py `
  test_tray_app.py test_features.py test_claude.py test_mimo.py test_reset_watch.py test_model_iq.py `
  test_agent_tasks.py test_settings.py
```

2026-10-16，本机 Windows 11 / Python 3.12.10 对代码版本 `d5a553a`（合入提交 `ea19ed0`）运行 **97 项测试，全部通过**。测试覆盖解析、计数去重、来源注册表、图标、卡片、刷新调度和预测计算；这是一份本机验证记录。

原生卡片另做过置顶全屏窗口测试：79 次连续采样位置稳定，未反复隐藏，键盘焦点保持不变。纯渲染用例覆盖不同缩放比例。2026-10-29 已实测 Claude Code Pro 的原生 5 小时/每周额度、本地会话和逐请求端到端 TPS；免费桌面端额度未接入。

尚未覆盖的场景包括 Windows 10 实机、所有显示器缩放组合、独占全屏游戏、各客户端未来版本，以及没有可观测本地记录的软件和独立网页。

## 参与维护

可通过 [Issues](https://github.com/ANTiqiuSama/CodexQuotaTray/issues) 报告问题，或提交 Pull Request。问题说明中提供 Windows、Python、客户端版本，复现步骤及脱敏后的错误字段。修复读取器时优先提供测试样例，说明该字段是原生值、计数还是推导值。

读取方案参考了 [Tokscale](https://github.com/junhoyeo/tokscale)、[Tallyburn](https://github.com/seoulpro/tallyburn)、[Grok Build](https://github.com/xai-org/grok-build) 和 [Codex 遥测说明](https://learn.chatgpt.com/docs/config-file/config-advanced)。本项目使用独立的 Python 读取器。

## 许可证

本项目自有源码与说明采用 [MIT License](LICENSE)，版权归 ANTiqiuSama。依赖库遵循各自的许可证；例如 pystray 的 LGPLv3、PyInstaller 的发行例外等，均不因本项目选择 MIT 而改变。分发打包版本时应保留所用依赖要求的许可材料。
