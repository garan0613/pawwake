# 🐾 Pawwake · 爪迹

**4.1.8 · Madeleine**

*Follow the pawprints back.*

Formerly AI Memory Gateway.

**让你的 AI 拥有长期记忆。**

一个轻量级转发网关，在你和 LLM 之间加一层记忆系统。支持任何 OpenAI 兼容客户端（Kelivo、ChatBox、NextChat 等）和任何 LLM 服务商（OpenRouter、OpenAI、本地 Ollama 等）。

Give your AI long-term memory. A lightweight proxy gateway that adds a memory layer between you and any LLM.

---

## ✨ 功能

- **自定义人设** — 可用 `system_prompt.txt` 提供默认人设，也可在 Dashboard 热更新运行时人设
- **长期记忆** — 自动从对话中提取关键信息，下次聊天时自动回忆相关内容
- **三层记忆架构** — 碎片（自动提取的原始记忆）→ 事件（整理合并后的完整事件）→ 核心（手动标记的重要记忆），支持 AI 生成可编辑的整理草稿、核心候选、手动合并、撤回合并和查看来源
- **分区缓存** — 自动管理对话上下文，通过 A/B 区轮转 + 摘要压缩，利用 prompt caching 大幅节省 token 费用。兼容 tool 调用消息
- **对话线管理** — 固定 session ID 实现跨平台对话衔接，支持多对话线切换、摘要编辑
- **对话记录** — 浏览、搜索、批量管理历史对话，支持 session 合并
- **Token 统计** — 自动记录每次对话的 token 消耗，按 session 汇总显示
- **双通道鉴权** — 程序 API 只接受请求头中的 `GATEWAY_SECRET`；Dashboard 使用独立密码登录和 HttpOnly 会话 Cookie，主密钥不会进入浏览器
- **预置记忆** — 把你想让 AI "一开始就知道"的事情批量导入
- **兼容性强** — 支持所有 OpenAI 格式的客户端和 API 服务商
- **记忆向量搜索（可选）** — 关键词 + 语义向量四维混合搜索，说"过年"能搜到"春节"。支持 OpenAI 兼容的 Embedding API
- **原始对话召回（可选）** — 从历史消息检索带上下文的稳定片段；分区模式按可配置 TTL 自动去重，可开启默认关闭的原文优先去重，raw API 支持调用方传入 session 与片段排除集合
- **设置面板** — 在 Dashboard 中直接管理所有运行时配置，热更新无需重启。支持模型列表动态拉取、可搜索下拉选择
- **零成本起步** — 可部署在 Render、Zeabur 等平台的免费额度内

## 🏗️ 架构

```
你的客户端（Kelivo / ChatBox / ...）
        ↓
   Pawwake · 爪迹（本项目）
   ├── 注入 system prompt（人设）
   ├── 搜索相关记忆 → 注入上下文
   ├── 转发请求 → LLM API
   └── 后台提取新记忆 → 存入数据库
        ↓
   LLM API（OpenRouter / OpenAI / Ollama / ...）
```

## 🚀 快速开始

### 第一阶段：纯转发网关（不需要数据库）

最简单的起步方式——先跑通网关，确认你的客户端能通过网关和 AI 对话。

**1. 准备文件**

上传整个项目目录即可。`main.py` 是启动入口，运行时还会加载同目录下的路由、数据库和共享状态模块；`system_prompt.txt` 是可选的 AI 人设文件。

**2. 修改人设**

编辑 `system_prompt.txt`，写入你想要的 AI 性格设定。Dashboard「设置」中保存的 System Prompt 会覆盖文件默认值，并从下一次请求开始生效。

**3. 部署到 Render（推荐）**

1. Fork 或上传代码到你的 GitHub 仓库
2. 注册 [Render](https://render.com)（免费层支持 Web Service，够用）
3. 创建 Web Service → 连接 GitHub 仓库 → Render 会自动检测 Dockerfile
4. 设置环境变量（Environment → Add Environment Variable）：

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `API_KEY` | 你的 LLM API Key；可在部署后从 Dashboard 设置 | `sk-or-v1-xxxx`（OpenRouter）|
| `API_BASE_URL` | LLM API 地址 | `https://openrouter.ai/api/v1/chat/completions` |
| `DEFAULT_MODEL` | 默认模型；留空时客户端必须显式传 `model` | 空 |
| `PORT` | 端口 | `8000` |
| `GATEWAY_SECRET`（强烈建议） | 程序 API 鉴权密钥，客户端通过 `X-Gateway-Key` 请求头发送 | 独立随机值 |
| `DASHBOARD_PASSWORD` | Dashboard 登录密码，不与网关密钥共用 | 独立强密码 |
| `SESSION_SECRET` | Dashboard 会话签名密钥，至少 32 字符且每次部署保持不变 | 独立随机值 |

5. 部署，访问你的网关地址看到 `{"status":"running"}` 就成功了

首次部署后先登录 `/dashboard`，在「设置 → 基础连接」填写 API 地址、Key 和默认模型；需要记忆提取时，再填写记忆模型。

> 从旧版本升级后，如果曾依赖内置的默认模型或记忆模型，也需要在 Dashboard 明确填写。默认模型留空且客户端未传 `model` 时返回 400；记忆模型留空时暂停提取、评分和整理。

可在本地分别运行三次下面的命令生成互不相同的随机值：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

从 4.0 升级时，先补齐 `DASHBOARD_PASSWORD` 和 `SESSION_SECRET` 再重新部署；缺少任一项时 Dashboard 会返回 503，程序 API 仍可继续用 `X-Gateway-Key` 访问。

> ⚠️ Render 免费层的服务在无活动时会休眠，第一次访问需要等几十秒唤醒，之后就正常了。其他支持 Docker 部署的平台（Zeabur、Railway、Fly.io 等）也可以，流程类似。

**4. 连接客户端**

以 Kelivo 为例：
- API 地址填：`https://你的网关地址.onrender.com/v1`
- API Key 填：随便填一个（网关会用自己的 key）
- 模型填：Dashboard 中设置的默认模型，或由客户端每次显式传入

### 第二阶段：加上记忆系统

在第一阶段基础上，加一个 PostgreSQL 数据库就能开启记忆功能。

**1. 创建数据库**

在 Render 中：Dashboard → New → PostgreSQL，创建一个免费的 PostgreSQL 实例，拿到连接字符串（Internal Database URL）。

> ⚠️ Render 免费 PostgreSQL 有 90 天有效期，到期前记得用导出功能备份数据。其他平台（如 [Neon](https://neon.tech)、[Supabase](https://supabase.com)）也提供免费 PostgreSQL，可按需选择。如果使用外部数据库，连接字符串末尾可能需要加 `?sslmode=require`。

**2. 添加环境变量**

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `DATABASE_ENABLED` | 数据库总开关；设为 `false` 时强制关闭记忆、分区缓存和对话召回，进入纯转发模式。恢复需改回 `true` 并重启 | `true` |
| `DATABASE_URL` | PostgreSQL 连接字符串 | `postgresql://user:pass@host:port/db` |
| `MEMORY_ENABLED` | 开启记忆 | `true` |
| `MEMORY_MODEL` | 记忆提取、评分、整理共用的模型（推荐便宜的小模型）；留空时暂停这三项后台调用 | 空 |
| `MEMORY_MAX_TOKENS（可选）` | 记忆提取、评分和整理单批次的输出上限。整理达到上限时会自动拆小批次；提取日志提示截断时可调高此项 | `4000` |
| `MAX_MEMORIES_INJECT` | 每次注入的最大记忆条数，`0` 禁用自动注入 | `15` |
| `MEMORY_SEEN_TTL_HOURS` | 分区模式按 `memories.id` 去重的小时数；成功响应后标记，`0` 关闭 | `6` |
| `MEMORY_SOURCE_DEDUPE_ENABLED` | 原文完整覆盖提取证据时跳过对应的一层碎片记忆 | `false` |
| `MIN_SCORE_THRESHOLD` | 记忆搜索最低分数阈值，低于此分数的记忆不注入（0=不过滤） | `0.15` |
| `MEMORY_EXTRACT_INTERVAL` | 记忆提取间隔（0=禁用/1=每轮/N=每 N 轮；N 越大调用越少） | `1` |
| `TIMEZONE_HOURS` | 时区偏移（小时），用于记忆注入时的日期显示 | `8`（UTC+8） |
| `FORCE_STREAM（可选）` | 强制所有请求走流式传输（解决部分客户端thinking不显示） | `false` |
| `REASONING_EFFORT（可选）` | 推理强度（low/medium/high），注入请求启用思维链。注意部分模型不支持 medium | 留空不注入 |

开启 `MEMORY_SOURCE_DEDUPE_ENABLED` 后，分区缓存、记忆注入与原始对话召回同时开启时，Pawwake 会做跨通道的原文优先去重：上线后新提取的一层碎片会记录逐条证据消息；本轮实际注入的原文完整覆盖全部证据时，该碎片退出记忆候选，后排仍达阈值的候选自然递补。来源未知、证据只覆盖一部分、正文被手动改过的碎片，以及事件和核心记忆都不会被压。已有记忆没有来源记录，升级后行为不变。该开关默认关闭，关闭时不会执行来源覆盖查询。

这与两种短窗 seen 账各管一件事：`MEMORY_SEEN_TTL_HOURS` 防止同一条记忆在当前 session 短期反复注入，`CONVERSATION_SEEN_TTL_HOURS` 防止同一原文片段短期反复召回，原文优先去重只消除同一轮里原文与自动碎片的重复。到期提醒始终保留。

**3. 重新部署**

部署后访问 `https://你的网关地址/dashboard`，输入 `DASHBOARD_PASSWORD` 登录；能正常打开管理页面就说明数据库连接成功。

**4. 导入预置记忆（可选）**

**方式一（推荐，不用碰代码）：** 写一个 `.txt` 文件，每行一条你想让 AI 知道的信息，然后打开 `https://你的网关地址/dashboard`，在「导入记忆」页面选择「纯文本导入」上传文件，系统会自动评估每条记忆的重要程度并导入。也可以勾选"跳过自动评分"节省 API 额度，之后在「记忆管理」页面手动调整权重。

**方式二（代码方式，开发者用）：**
1. 复制 `seed_memories_example.py` 为 `seed_memories.py`
2. 修改里面的记忆条目，写入你想让 AI 一开始就知道的信息
3. 部署后访问 `https://你的网关地址/import/seed-memories`，看到 `"status": "done"` 就导入成功了

**5. 管理记忆（可选）**

打开 `https://你的网关地址/dashboard` 可以查看所有记忆，支持搜索、编辑内容、调整权重、单条删除和批量删除，以及导入/导出备份。

> 💡 Dashboard 登录与程序 API 分开。浏览器只使用 Dashboard 密码和 `HttpOnly + Secure + SameSite=Strict` 会话 Cookie；客户端请求通过 `X-Gateway-Key: 你的密钥` 鉴权。不要把任何密钥放进 URL。

### 第三阶段：分区缓存（省 token 费）

分区缓存让网关自动管理对话上下文，通过 A/B 区轮转 + 摘要压缩利用 prompt caching，大幅降低 token 开销。

**工作原理：**

```
[人设区]    system prompt，永远不变     ← 缓存命中
[摘要区]    历史压缩摘要               ← 正常轮次命中
[历史A区]   15轮原始消息               ← 正常轮次命中
[历史B区]   当前周期消息               ← 通过lookback命中
[当前输入]  时间+记忆+对话片段+用户消息  ← 不缓存（每次不同）
```

首次轮转要等 A、B 两区各装满 15 轮，也就是累计 30 轮；此后每新增 15 轮轮转一次。轮转时 A 区压缩成摘要追加到摘要区（`CACHE_SUMMARY_MODEL` 留空则不生成摘要，A 区直接滑出），B 区升级为新的 A 区。正常轮次 90% 的 token 走缓存读取（0.1x 价格）。

记忆召回与原始对话召回都放在最后的当前输入中，位于所有缓存断点之后。每轮召回结果变化只影响本次不缓存的输入，不会改写人设、摘要或 A/B 历史区。

**添加环境变量：**

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `CACHE_PARTITION_ENABLED` | 分区缓存开关 | `true` |
| `CACHE_PARTITION_X` | 轮转周期（轮数）。1轮 = 一次用户发言 + AI回复。B 区攒满 X 轮触发，首次摘要生成需先装满 A、B 两区共 2X 轮 | `15` |
| `CACHE_SUMMARY_MODEL` | 摘要模型。**留空 = 不生成摘要**，轮转时旧消息直接滑出上下文（纯轮转模式）。从旧版本升级的用户注意：旧版此项有默认模型，新版默认为空，需要摘要请显式配置。不建议使用推理模型（思考可能耗尽输出token导致摘要为空） | 空 |
| `CACHE_SUMMARY_MAX_TOKENS`（可选） | 摘要的输出上限，日志出现"摘要生成失败: 模型返回空content"且 `finish_reason=length` 时调高此项 | `2000` |
| `CACHE_SUMMARY_BUDGET_CHARS`（可选） | 摘要区总字符预算。轮转后摘要区超过此值时，把最旧的几段合并压缩成一段基础摘要，摘要区不再无限增长。`0` = 不折叠（旧行为）。折叠时固定保留最近 2 段旧摘要和本轮新生成的摘要原文；折叠失败时本次轮转整体不生效，下次请求重试。升级后默认即有界 | `8000` |
| `PARTITION_SESSION_ID` | 固定的 session ID | `my-thread` |
| `CACHE_PARTITION_TRIGGER`（可选） | 轮转触发方式：`rounds`（按轮次，默认）或 `time`（按时间窗口，适合微信等消息频率高的场景） | `rounds` |
| `CACHE_PARTITION_WINDOW`（可选） | 时间窗口（分钟），仅 `trigger=time` 时生效。窗口内的消息不触发摘要压缩 | `30` |
| `CACHE_MAX_ROTATIONS`（可选） | 时间窗口模式下单次请求最大轮转次数 | `2` |
| `CACHE_TTL`（可选） | 缓存有效期：`5m`（默认）或 `1h`。Anthropic 官方定价：5m 写入 1.25x、1h 写入 2x，读取都是 0.1x。消息间隔经常超过 5 分钟的慢聊场景（比如挂着微信/TG 等回复）建议 `1h`，缓存不会中途过期。OpenRouter 会原样透传此参数。设置面板可热更新 | `5m` |

> 💡 **记忆与分区缓存可以独立开关。** `MEMORY_ENABLED=false` 会停止记忆检索、注入和提取；只要 `CACHE_PARTITION_ENABLED=true`，对话仍会落库并继续摘要轮转。分区模式由网关托管历史，因此必须保持数据库可用。

**管理面板：**

部署后在 Dashboard 的「🔗 对话线」页面可以：
- 查看当前活跃对话线的状态（摘要长度、轮转进度）
- 重命名对话线 ID（关联的对话记录和 token 统计自动迁移）
- 查看、编辑、清空摘要内容
- 新建对话线（可选择继承已有摘要）
- 一键切换活跃对话线（运行时生效，不用重启）

### 第四阶段：关闭记忆（应急）

如果记忆系统出问题，把环境变量 `DATABASE_ENABLED` 改回 `false` 即可退回纯转发模式，不需要改代码。

## 📁 文件说明

```
pawwake/
├── main.py                    # 应用组装、生命周期与启动入口
├── shared.py                  # 可热更新配置、共享运行态与人设缓存
├── auth.py                    # 网关请求头与 Dashboard Cookie 鉴权
├── partition_engine.py        # 分区上下文、轮转与摘要
├── memory_pipeline.py         # 记忆注入、对话召回与后台提取
├── memory_extractor.py        # AI 记忆提取器
├── memory_consolidator.py     # 自动整理预览与核心候选
├── routes/                    # 按领域拆分的 HTTP 路由
│   ├── chat.py                # 健康检查与 OpenAI 兼容聊天
│   ├── dashboard.py           # Dashboard、模型列表与设置
│   ├── memories.py            # 记忆管理、导入、整理接口与补算
│   ├── conversations.py       # 对话管理与片段检索
│   └── partition.py           # 分区状态与对话线管理
├── db/                        # PostgreSQL 数据层
│   ├── core.py                # 连接池、表结构与网关配置
│   ├── search.py              # 分词、向量、召回与补算
│   ├── conversations.py       # 对话、分区状态与 Token 统计
│   └── memories.py            # 记忆存取、备份与生命周期
├── database.py                # 4.x 旧导入兼容入口，5.0 删除
├── system_prompt.txt          # 你的 AI 人设（自行编辑）
├── seed_memories_example.py   # 预置记忆示例
├── requirements.txt           # Python 依赖
├── Dockerfile                 # 容器配置
├── templates/                 # 页面模板（Dashboard 界面）
│   ├── dashboard.html         # 主控制台页面
│   ├── login.html             # Dashboard 登录页
│   └── ...
├── static/                    # 静态资源
│   ├── css/                   # 样式文件
│   └── js/                    # 前端脚本
├── LICENSE                    # GNU AGPLv3 许可证
└── README.md                  # 本文件
```

## 🔧 API 接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 健康检查，查看网关状态 |
| `/v1/chat/completions` | POST | 核心转发接口（OpenAI 兼容） |
| `/api/memories` | POST | 显式写入单条记忆；需 `X-Gateway-Key`，支持 `external_id` 幂等重试 |
| `/v1/models` | GET | 模型列表 |
| `/dashboard/login` | GET/POST | Dashboard 独立密码登录 |
| `/dashboard/logout` | POST | 退出 Dashboard 登录 |
| `/dashboard` | GET | 管理控制台（记忆、对话、对话线一体化界面） |
| `/import/seed-memories` | GET | 执行预置记忆导入（开发者用） |
| `/api/memories` | GET/POST | 获取记忆，或显式创建一条记忆 |
| `/api/memories/search` | GET/POST | 混合搜索记忆；私密查询推荐 POST JSON `{"q":"..."}` |
| `/api/memories/consolidate` | POST | 按当前所选活跃记忆，或日期范围内的活跃碎片与事件异步生成可编辑草稿，不写数据库 |
| `/api/memories/consolidate/status` | GET | 查询整理草稿生成状态与结果 |
| `/api/memories/merge` | POST | 手动合并多条记忆 |
| `/api/memories/core-candidates` | GET | 返回最多 20 条可解释的核心记忆候选，不自动升级 |
| `/api/memories/check-duplicate` | POST | 记忆去重检查 |
| `/api/memories/cleanup-fragments` | POST | 清理 N 天前的归档碎片 |
| `/api/memories/layer-stats` | GET | 获取各层记忆统计 |
| `/api/memories/{id}` | PUT | 更新记忆（支持 content / importance / title / layer） |
| `/api/memories/{id}` | DELETE | 删除记忆（`?soft=true` 软删除） |
| `/api/memories/{id}/promote` | POST | 升级为核心记忆 |
| `/api/memories/{id}/restore` | POST | 恢复已归档的记忆 |
| `/api/memories/{id}/revert-merge` | POST | 撤回合并，恢复原始碎片 |
| `/api/conversations` | GET | 分页获取对话列表；`deleted=true` 查看回收站 |
| `/api/conversations/{id}/messages` | GET | 获取指定对话的消息列表；`deleted=true` 查看已删除消息 |
| `/api/conversations/{id}` | DELETE | 将指定对话移入回收站 |
| `/api/conversations/batch-delete` | POST | 批量将对话移入回收站 |
| `/api/conversations/{id}/restore` | POST | 恢复整段删除的对话消息 |
| `/api/conversations/batch-restore` | POST | 批量恢复整段删除的对话消息 |
| `/api/conversations/{id}/permanent` | DELETE | 永久删除该对话的回收站内容 |
| `/api/conversations/batch-permanent-delete` | POST | 批量永久删除回收站内容 |
| `/api/chat/messages/{id}` | DELETE | 将单条消息移入回收站 |
| `/api/chat/messages/{id}/restore` | POST | 恢复单条已删除消息 |
| `/api/chat/messages/{id}/permanent` | DELETE | 永久删除单条回收站消息 |
| `/api/chat/search-fragments` | GET/POST | 无状态检索历史对话片段；POST 支持数组形式的排除参数 |
| `/api/admin/merge-sessions` | POST | 合并多个 session 到目标 session |
| `/api/admin/rebuild-conversation-search` | POST | 补齐对话 TSV 并唤醒向量补算 |
| `/api/admin/conversation-embedding-status` | GET | 查询对话检索索引与向量补算状态 |
| `/api/admin/backfill-memory-embeddings` | POST | 启动记忆 embedding 补算（后台异步） |
| `/api/admin/backfill-memory-embeddings/status` | GET | 查询补算进度 |
| `/api/models` | GET | 获取可用模型列表（根据 API 服务商自动适配） |
| `/api/settings` | GET | 获取所有运行时配置（设置面板用） |
| `/api/settings` | PUT | 保存配置（写入数据库 + 热更新，立即生效无需重启） |
| `/api/partition/status` | GET | 获取分区缓存当前状态 |
| `/api/partition/threads` | GET | 列出所有对话线 |
| `/api/partition/summary` | PUT/DELETE | 编辑/清空对话线摘要 |
| `/api/partition/thread` | POST | 新建对话线 |
| `/api/partition/thread/rename` | PUT | 重命名对话线 ID |
| `/api/partition/switch` | POST | 切换活跃对话线 |

## 🌐 支持的 LLM 服务商

只要兼容 OpenAI 聊天格式就行。改 `API_BASE_URL` 环境变量即可切换：

| 服务商 | API_BASE_URL |
|--------|-------------|
| OpenRouter | `https://openrouter.ai/api/v1/chat/completions` |
| OpenAI | `https://api.openai.com/v1/chat/completions` |
| Ollama（本地） | `http://localhost:11434/v1/chat/completions` |
| 其他兼容服务 | 查阅对应文档 |

> ⚠️ 部分 Gemini preview 模型（如 `gemini-3-flash-preview`）可能存在流式输出兼容性问题导致空回复，建议使用正式版模型（如 `gemini-2.5-flash`）。

## 💡 记忆系统原理

1. **你发消息** → 网关从数据库搜索相关记忆
2. **记忆注入** → 分区缓存开启时，相关记忆随当前输入动态注入；关闭时拼接到 system prompt 后面
3. **AI 回复** → 网关边转发边捕获完整回复
4. **后台提取** → 达到提取间隔时，用小模型（如 Haiku）从近期逻辑轮中提取关键信息
5. **存入数据库** → 下次对话时可以检索到

分区缓存开启时，网关按当前 session 的逻辑轮数触发提取，并从数据库托管的权威历史中截取最近 N 个逻辑轮；含多条 tool 消息的调用过程仍只算一轮。分区缓存关闭时，提取上下文来自本次客户端请求实际携带的非 system 消息。两种模式都会附上本轮最终 AI 回复，system prompt、网关注入的记忆与历史对话片段不会送给提取模型。`MEMORY_EXTRACT_INTERVAL` 设为 `0` 时禁用自动提取，设为 `1` 时每轮提取，设为 `N` 时每 N 轮提取一次。把 N 调大可以减少提取调用次数，但单次覆盖的近期消息也会相应增多。

> **关于向量搜索：** 当前版本支持可选的记忆向量搜索功能。默认使用 jieba 中文分词 + 关键词匹配（ILIKE），适合大多数场景。如果需要语义搜索（说"过年"能搜到"春节"），可以设置 `MEMORY_VECTOR_ENABLED=true` + `EMBEDDING_API_KEY`，系统会同时走关键词和向量两路搜索，四维加权排序。混合排序中，关键词分按词在记忆库里的稀有度加权，命中少见的词更有分量；语义分的归一化上界不低于 `MEMORY_SEMANTIC_THRESHOLD`，整池都是弱相似候选时不会把最像的那条抬成满分。支持任何 OpenAI 兼容的 Embedding API（OpenAI、Jina、Voyage、本地 Ollama 等）。如果数据库支持 pgvector 扩展会自动启用，否则回退到 Python 端计算余弦相似度。

**向量搜索环境变量（可选）：**

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `MEMORY_VECTOR_ENABLED` | 记忆向量搜索开关 | `false` |
| `EMBEDDING_API_KEY` | Embedding API Key（必需） | 无 |
| `EMBEDDING_BASE_URL` | Embedding API 地址 | `https://api.openai.com/v1` |
| `EMBEDDING_MODEL` | Embedding 模型 | `text-embedding-3-small` |
| `EMBEDDING_DIM` | 向量维度；首次向量请求会检测实际返回值，需迁移时到 Dashboard 确认 | `256` |
| `MEMORY_HW_KEYWORD` | 混合搜索：关键词权重 | `0.35` |
| `MEMORY_HW_SEMANTIC` | 混合搜索：语义相似度权重 | `0.35` |
| `MEMORY_HW_IMPORTANCE` | 混合搜索：重要程度权重 | `0.15` |
| `MEMORY_HW_RECENCY` | 混合搜索：时间衰减权重 | `0.15` |
| `MEMORY_SEMANTIC_THRESHOLD` | 向量相似度阈值 | `0.7` |

开启后，新记忆会自动计算 embedding。已有记忆可以在 Dashboard 记忆管理页面点击「开始补算」一键补算。

**原始对话召回（可选）：**

对话召回默认关闭。开启后，新写入、编辑、重复提交覆盖和导入都会同步维护关键词索引，旧向量会失效并由可续跑 worker 补算。混合排序使用关键词、语义和新近度；语义候选先按原始余弦相似度过滤，再做 min-max 归一化，避免弱候选池把无关头名拉成满分。

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `CONVERSATION_RECALL_ENABLED` | 对话召回总开关；关闭时不写索引、不补算、不检索 | `false` |
| `MAX_CONVERSATIONS_INJECT` | 分区模式每轮最多自动注入的历史对话片段总数；优先每条线各取一个，再轮流补足，每条线最多 5 个；`0` 关闭自动注入 | `3` |
| `CONVERSATION_SEEN_TTL_HOURS` | 分区模式按 `fragment_id` 去重的小时数；过期后允许再次召回，`0` 关闭 seen 去重 | `6` |
| `CONVERSATION_MIN_SCORE_THRESHOLD` | 对话语义候选的最低原始余弦相似度，与记忆阈值分开 | `0.7` |
| `CONVERSATION_HW_KEYWORD` | 对话混合搜索：关键词权重 | `0.45` |
| `CONVERSATION_HW_SEMANTIC` | 对话混合搜索：语义相似度权重 | `0.35` |
| `CONVERSATION_HW_RECENCY` | 对话混合搜索：时间衰减权重 | `0.2` |

分区模式只允许召回当前 session 中已经滑出原文上下文的旧前缀；仍在 A/B 区的消息继续排除，避免重复注入。其他 session 照常参与检索，并按 `CONVERSATION_SEEN_TTL_HOURS` 保存每个成功注入的稳定 `fragment_id` 及其独立时间戳。TTL 内不会重复注入同一片段，过期后会自动放行；`0` 关闭 seen 去重。普通非流式请求仅在上游返回 200 后标记；流式请求仅在 200 响应自然结束后标记，客户端取消或上游失败不会吞掉片段。非分区模式不会自动注入。

raw API 不保存 seen 状态。调用方需要把上次返回的 `fragment_ids` 作为下次的 `exclude_fragment_ids` 传回，也可以用 `exclude_session_ids` 排除整条对话线。POST 示例：

```json
{
  "q": "上次聊到的旅行计划",
  "mode": "hybrid",
  "max_sessions": 3,
  "exclude_session_ids": ["current-thread"],
  "exclude_fragment_ids": ["v1:0123456789abcdef0123456789abcdef"]
}
```

## ❓ 常见问题

**Q: 部署后访问显示 502 或服务无响应？**
A: 检查端口设置。Render 默认用 `PORT` 环境变量，确保设置为 `8000`（和 Dockerfile 里一致）。如果用其他平台，注意端口是否匹配。

**Q: Dashboard 能打开，但保存设置时提示 `Invalid request origin.`？**
A: 更新到包含此修复的版本并重新部署，不需要新增环境变量。若自建反向代理后仍报错，请确认代理保留了原始 `Host`，并通过 `X-Forwarded-Proto` 告诉后端浏览器使用的协议。Nginx 可在对应的 `location` 中加入：

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto $scheme;
```

**Q: 数据库连接失败？**
A: 如果数据库和网关不在同一个平台，连接字符串末尾可能需要加 `?sslmode=require`。分区缓存开启时，数据库不可用会让聊天端点返回明确的 `503 partition_database_unavailable`，避免用残缺历史继续对话。分区缓存关闭时，网关会用文件默认人设继续纯转发。

**Q: 记忆会越来越多影响性能吗？**
A: 每次最多注入 15 条记忆（可调），不会无限增长地消耗 token。分区模式按 session 的逻辑轮提取近期对话，非分区模式使用客户端本次携带的上下文；两者都不会发送整条会话。把 `MEMORY_EXTRACT_INTERVAL` 调大可以减少提取调用次数，设为 `0` 可完全禁用自动提取。

**Q: 能用免费额度跑吗？**
A: Render 免费层支持 Web Service + PostgreSQL，网关资源消耗很低，够用（注意免费 PostgreSQL 有 90 天期限）。也可以用 Neon 或 Supabase 的免费 PostgreSQL 作为长期方案。LLM API 费用另算（推荐 OpenRouter，按量付费）。

**Q: 怎么备份记忆？换平台会丢数据吗？**
A: 打开 `https://你的网关地址/dashboard`，在「导出备份」页面下载所有记忆的 JSON，建议定期备份。迁移到新平台后，在「导入记忆」页面选择「JSON 备份恢复」上传导出的文件即可。

**Q: 不会写代码能搞吗？**
A: 能。这个项目的第一个部署者就是不会写代码的——代码是 AI 写的，部署是她自己看文档搞定的。

## 📋 更新日志

### v4.1.8 · Madeleine（2026-10-08）

- **模型改为按需设置** — `DEFAULT_MODEL` 和 `MEMORY_MODEL` 不再带内置值，新旧部署都可在 Dashboard 按需填写；聊天请求未带模型且 Dashboard 没有默认模型时返回 400，`/v1/models` 返回空列表。
- **记忆模型留空即暂停** — 停止记忆提取、评分和整理；对话照常保存，留空期间不会补提取。
- **模型列表跟随服务商** — Google 一次读取最多 1000 个模型，只显示可对话项并将新版 Gemini 排在前面；OpenAI 展示接口返回的全部型号，可能包含不能对话的模型。拉取失败时直接报错，不再显示过时的备用列表。

### v4.1.7 · Madeleine（2026-09-27）

- **向量维度自动检测** — 接口拒绝 `dimensions` 时去参重试，记录模型实际返回维度；Dashboard 显示检测值，并可重新检测。
- **报错与维度提示** — 日志显示服务商错误码和说明；返回维度不符时跳过写入，并提示应填的值。
- **换模型或维度安全迁移** — 待确认期间记忆和对话只走关键词；Dashboard 确认后清空旧向量，列宽变化时同时重建，各路功能开启且配有向量密钥时在后台补算。仅换地址且确认为同一模型时可保留旧向量；两表都没有向量时免清空确认。补算按条数调用向量接口。
- **旧库平滑升级** — 首次升级以当前设置建立向量来源基线，已有向量不会因缺少旧记录而被清空；升级前若曾同维换模型，旧向量来源无法追溯。
- **向量补算进度** — 记忆和对话记录页常显已有向量、待补条数和进度，对话记录页新增手动唤醒补算入口；记忆或向量功能未开、未配向量密钥或向量待迁移时提示原因。记忆补算只处理活跃记忆，从归档恢复且缺向量的记忆需再点一次补算。

### v4.1.6 · Madeleine（2026-09-25）

- **弱相似候选不再被抬成满分** — 混合搜索以候选池内最低相似度为零点，并保证归一化上界不低于 `MEMORY_SEMANTIC_THRESHOLD`，避免整池相似度都偏低时头名仍拿到满分。
- **关键词按稀有度加权** — 混合搜索按关键词在活跃记忆库中的稀有度计分，并在截取候选前完成排序；命中少见关键词的记忆更靠前，只碰到常见词的候选不再被批内放大。

### v4.1.5 · Madeleine（2026-09-22）

- **混合搜索保留关键词候选的真实相似度** — 语义阈值只决定纯语义候选能否进入候选池；关键词命中的记忆无论相似度高低都按真实相似度参与排序，不再因低于阈值被记为零分而被同批高相似候选挤出前列。
- **可选的原文优先去重** — `MEMORY_SOURCE_DEDUPE_ENABLED` 默认关闭；开启后，自动提取的一层碎片记录逐条证据消息，分区模式本轮召回原文完整覆盖全部证据时，记忆检索跳过该碎片，关键词与向量候选都在排序上限前排除，原有阈值继续生效。
- **保留高层价值与人工修订** — 事件和核心记忆始终保留；手动实改碎片正文后保留来源记录并退出原文去重，只改标题、重要度或层级不受影响。
- **继承来源记录** — 合并产物在全部父记忆来源已知时继承去重后的来源消息 ID，任一来源未知则整体保持未知；备份恢复仅在同一数据库实例保留本地消息 ID，跨库导入安全降级为来源未知。

### v4.1.4 · Madeleine（2026-09-21）

- **对话回收站** — 删除整段、批量删除与删除单条消息均改为可恢复软删除；整段恢复同时带回删除前的摘要状态，删后新增的对话按原顺序继续摘要，旧段被改动时自动安全重建。搜索、召回、摘要历史读取与导出只使用活跃记录。升级时自动迁移表结构，回收站不自动清理，永久删除需在 Dashboard 手动确认。
- **补全当前线原文召回** — 分区指针之前、已经滑出原文上下文的当前线旧消息可以重新召回；仍在 A/B 区的内容继续排除，避免重复注入。
- **让最大注入数按片段生效** — 自动召回先为每条命中对话线保留一个片段，再轮流补足全局上限；单线用户也能按设置注入多个旧片段，每条线最多 5 个。

### v4.1.3 · Madeleine（2026-09-13）

- **统一自动整理流水线** — 当前勾选可包含任意数量的活跃碎片、事件和核心记忆；按日期只选择活跃碎片与事件，两种范围共用按事件分组、来源覆盖校验和拆批逻辑
- **保护核心记忆确认** — 含核心来源的草稿明确标记并退出“全部确认”，只能逐组确认或编辑，避免日期范围与批量确认意外归档核心记忆

### v4.1.2 · Madeleine（2026-09-12）

- **限制后台自动接管层级** — 自动提取只允许淘汰碎片记忆；事件记忆和核心记忆保持活跃，新事实改存为独立碎片并记录被拦截的接管
- **修正时间窗口轮转判断** — 时间触发改为比较 A 区最新消息与当前时间，持续对话也会按窗口正确轮转
- **限制摘要区总量** — 新增可热更新的 `CACHE_SUMMARY_BUDGET_CHARS`（默认 `8000`，设为 `0` 保留旧行为）；超出预算时折叠最旧摘要，折叠结果无效则放弃本轮轮转并在下次请求重试
- **新增到期提醒** — 记忆支持 `remind_at` 与 `reminder_delivered_at`；到期项每次只交给一个请求并注入下一次对话，响应成功结束才标记送达，失败或取消后交给后续请求重试
- **新增自动整理预览** — Dashboard 可整理当前选中的混合层级记忆，也可按日期批量整理碎片；AI 先生成可编辑草稿，用户逐条确认后才写入数据库，核心候选仍只给建议
- **强化确认合并安全性** — 确认前校验来源 ID，事务内锁定并复验每条来源仍存在、活跃且没有未送达提醒，再创建新记忆并归档来源
- **统一整理日期口径** — 合并来源没有 `event_date` 时，按 `TIMEZONE_HOURS` 将 `created_at` 换算为 Dashboard 本地日期，避免午夜附近的碎片被记到相邻 UTC 日期
- **收紧整理辅助查询范围** — 所选记忆只读取指定 ID，核心候选的过滤、排序与上限下推到 SQL
- **统一辅助模型请求** — 提取、评分、整理、摘要与草稿共用 `shared.post_chat_completion`，统一 OpenAI 兼容请求头，并为 OpenRouter 补齐归属请求头

### v4.1.1 · Madeleine（2026-08-24）

- **记忆语义阈值默认上调** — `MEMORY_SEMANTIC_THRESHOLD` 从 `0.5` 调整为 `0.7`，减少弱向量候选进入混合排序；只影响未显式配置该值的部署，`.env` 或 Dashboard 已保存的值继续保留，老用户需要时可自行调整
- **记忆注入短窗去重** — 分区模式按 session 记录实际成功注入的 `memories.id`，默认 6 小时内不重复注入；逐条独立过期并由后排候选递补，客户端取消或上游失败不落账，`MEMORY_SEEN_TTL_HOURS=0` 可关闭
- **BREAKING：移除重复的自动流水线开关** — 删除 `MEMORY_EXTRACT_ENABLED`；原先设为 `false` 的部署，升级前请改设 `MEMORY_EXTRACT_INTERVAL=0` 与 `MAX_MEMORIES_INJECT=0`。前者禁用自动提取，后者禁用自动注入；消息持久化和手动记忆管理不受影响。旧环境仍保留该变量时，启动日志会说明它已被忽略并给出两个替代键

### v4.1.0 · Madeleine（2026-08-24）

- **提取候选检索** — 自动去重改用相关 top K 与最新 M 条活跃记忆的只读并集；不改写访问时间，不混入重要度、时间或召回阈值，向量不可用时退回关键词与最新记忆
- **自动冲突接管** — 提取模型在同一次调用中区分新增、重复和取代；只有候选集内仍活跃的旧记忆可被原子接管，伪造或过期 ID 只会把有效内容按新增保存
- **可撤销版本链** — 被取代原文保留并退出默认召回，Dashboard 可查看版本链并恢复旧版本；v3 备份完整携带并重映射后继关系，版本链中的记忆禁止永久删除

### v4.0.5 · Madeleine（2026-08-23）

- **数据库总闸** — 新增默认开启的 `DATABASE_ENABLED`；关闭后统一压制三个数据库子能力、跳过连接与初始化，并保留纯转发聊天和模型列表
- **单条记忆写入 API** — 新增 `POST /api/memories`，供 MCP、脚本、日记摘要和迁移工具显式写入记忆；严格校验字段并只接受 `X-Gateway-Key`
- **幂等外部 ID** — 可选 `external_id` 使用部分唯一索引防止重复写入，重试会返回原记录并标记 `inserted=false`
- **模块化内部结构** — 路由、共享运行态、分区缓存、记忆流水线和数据库操作按领域拆分；除 4.0.5 版本标识外，启动命令、57 条产品路由、数据库结构与既有响应保持不变。老用户升级请用 `Pawwake-4.0.5/` 整目录覆盖旧文件，不要只替换 `main.py`；启动时自动补建索引，无需手动迁移。

### v4.0.3 · Madeleine（2026-08-18）

- **活跃与归档操作分流** — 记忆管理拆分为「活跃记忆」和「已归档」页面；单条与批量删除默认可恢复，已归档记忆可批量恢复或永久删除，服务端按真实状态守卫并返回实际处理数量
- **合并来源完整性保护** — 仍被整理记忆引用的来源不能永久删除；撤回合并会在同一事务中核对并恢复全部来源，30 天清理会同步结束受影响整理记忆的撤回能力并给出明确回执
- **备份断裂检测与修复** — 导出前校验所有 `merged_from` 引用，发现历史断裂时阻止生成不完整备份；Dashboard 可在确认后保留合并记忆、清除失效来源关系，再重新导出
- **内部 ID 隐藏** — 记忆列表、搜索结果、合并来源和导入回执统一使用连续序号或数量描述，不再向 Dashboard 使用者暴露数据库 ID
- **搜索降级可见** — 记忆搜索返回本次真实使用的模式；向量搜索未生效时明确提示已按关键词搜索，避免把降级结果误认成语义搜索

### v4.0.2 · Madeleine（2026-08-11）

- **Gemini 工具签名透传** — 流式工具调用完整保留 `tool_call` 与 `function` 中的服务商扩展字段，分区缓存落库重建后不再丢失 Gemini `thought_signature`
- **召回时间前缀本地化** — 对话召回片段的日期前缀按 `TIMEZONE_HOURS` 换算为本地时间并精确到分钟（原样截取 UTC 日期时，凌晨对话会显示成前一天）；时间戳解析失败自动退回旧格式

### v4.0.1 · Madeleine（2026-08-10）

- **Dashboard 独立登录** — 新增 `DASHBOARD_PASSWORD` 登录页，使用稳定 `SESSION_SECRET` 签发 12 小时 `HttpOnly + Secure + SameSite=Strict` 会话 Cookie
- **主密钥退出浏览器** — 删除 `?gateway_key=` 鉴权和前端请求头注入，程序 API 只接受 `X-Gateway-Key` 请求头；Cookie 鉴权的写操作强制校验同源 `Origin`
- **私密记忆检索** — `/api/memories/search` 新增 POST JSON 形状，查询不再需要把本轮消息放进 URL
- **记忆提取间隔修复** — 分区模式改用当前 session 的权威历史按逻辑轮触发和截取，客户端只发送最新消息时也不会漏掉间隔内的对话；tool 调用过程只算一轮
- **去重参照过滤** — 自动提取只把活跃记忆放进已有信息列表，软归档与合并后停用的碎片不再阻挡新信息提取
- **仓库链接更新** — Dashboard 的 GitHub 入口同步到更名后的 `garan0613/pawwake`

### v4.0 · Madeleine（2026-08-10）

- **原始对话召回** — 新增默认关闭的历史原文召回，可从过往对话中找回带上下文的稳定片段，与摘要记忆互补
- **中文混合搜索** — 统一中文写入与查询词表，组合关键词、语义相似度和新近度排序；语义候选先过原始余弦阈值，减少弱结果被归一化抬高
- **可续跑向量补算** — 新消息、编辑、重复提交覆盖和导入会同步维护关键词索引；旧对话的 embedding 可后台分批补算，失败可续跑
- **两层召回去重** — 始终排除当前活跃 session；历史片段按稳定 `fragment_id` 和独立时间戳去重，TTL 可配置，`0` 可关闭 seen 去重
- **成功后再记 seen** — 非流式请求只在上游返回 200 后标记，流式请求只在自然结束后标记；取消、断流或上游失败均保留重试机会
- **Dashboard 对话召回设置** — 新增最大注入数、关键词/语义/时效性权重、语义阈值和片段去重时长，保存后热更新并在重启时恢复，所有数值均支持 `0`
- **项目更名与协议更新** — AI Memory Gateway 正式更名为 **Pawwake · 爪迹**，4.0 代号 **Madeleine**，许可证更新为 GNU AGPLv3

### v3.9（2026-08-02）

- **关闭记忆后仍可管理配置** — Dashboard 不再因 `MEMORY_ENABLED=false` 整页消失；数据库初始化、面板 override 恢复和活跃对话线恢复均与记忆开关解耦
- **System Prompt 热更新修复** — 每次聊天请求只解析一次 DB 优先的 System Prompt，分区、记忆增强和纯人设路径共用同一个最终值
- **记忆与分区缓存独立** — 关闭记忆后，只要分区缓存开启，对话仍会落库并继续摘要轮转
- **分区故障响亮失败** — 分区缓存开启且数据库不可用时立即返回 503，避免静默丢历史与上下文
- **摘要失败可重试** — 配置了摘要模型且生成失败时保留当前 A 区；摘要模型留空时仍按原语义执行纯轮转

### v3.8（2026-07-30）

- **跨日记忆整理安全分批** — 日期范围按本地日期逐日处理；单日输出达到上限或碎片 ID 覆盖不完整时自动二分重试，避免多天内容挤进同一个 JSON 数组后被截断
- **截断检测与完整性校验** — 读取 `finish_reason` 和 completion usage，截断响应不再进入 JSON 修复；每个批次必须完整且唯一地覆盖输入碎片 ID
- **原子写入与准确日期** — 全范围整理成功后才在一个数据库事务中创建事件并归档来源碎片；任何批次或写入失败都保留全部原始碎片。事件日期按碎片实际本地日期记录
- **JSON 修复不再裁切** — 需要修复语法时传入完整模型响应，移除会静默丢弃后半段的 2000 字符裁切

### v3.7（2026-07-07）

- **缓存 TTL 可配置** — 新增 `CACHE_TTL` 变量（`5m` 默认 / `1h`），所有缓存断点统一带上对应有效期。消息间隔经常超过 5 分钟的慢聊场景用 `1h` 可避免缓存中途过期反复重建。支持设置面板热更新，OpenRouter 链路原样透传

### v3.6（2026-05-10）

- **时间窗口模式** — 分区缓存新增 `CACHE_PARTITION_TRIGGER=time` 模式。按时间而非轮次触发摘要压缩，适合微信等消息频率高的场景（一条消息算一轮，15轮可能就几分钟）。窗口时间通过 `CACHE_PARTITION_WINDOW` 配置，默认30分钟
- **非 Claude 模型兼容** — 分区缓存模式下自动检测模型，非 Anthropic Claude 系列的模型在发送前自动剥离 `cache_control` 字段并将 content 降级为纯字符串格式，解决智谱 GLM 等模型因不认识 `cache_control` 而报错或丢上下文的问题
- **整理记忆时区修复** — 修复按日期整理记忆时 `DATE(created_at)` 使用 UTC 日期而 Dashboard 显示北京时间导致的日期偏移。改为根据 `TIMEZONE_HOURS` 将本地日期转换为 UTC 时间范围查询

### v3.5（2026-05-06）

- **设置面板** — Dashboard 新增「设置」页面，所有运行时配置可在网页端直接修改，热更新立即生效无需重启
  - 基础连接（API 地址、Key、默认模型）
  - 记忆系统（开关、提取模型、注入条数、分数阈值、提取间隔）
  - 缓存分区（开关、轮转周期、摘要模型）
  - 向量搜索（开关、Embedding API Key/Base URL/模型/维度）
  - 记忆搜索权重（四维权重滑块 + 语义阈值）
  - 其他（强制流式、推理强度）
  - System Prompt（在线编辑，实时字数统计）
- **模型列表 API** — 新增 `/api/models` 端点，根据 API 服务商（OpenRouter/Google/OpenAI）自动拉取可用模型列表，设置面板的模型选择框支持搜索过滤
- **Dashboard 美化** — Emoji 图标全部替换为内联 SVG（Lucide 风格），配色从冷灰青绿迁移到暖奶白玫瑰粉，全局输入框统一样式

### v3.3（2026-05-05）

- **三层记忆架构** — 碎片（layer 1，自动提取的原始记忆）→ 事件（layer 2，AI 整理合并后的完整事件）→ 核心（layer 3，手动标记的重要记忆）。数据库自动迁移，老数据默认为碎片层
- **记忆整理** — 选择日期范围，一键调用 AI 将碎片合并为事件记忆。异步执行，整理 prompt 保留原文中的主观感受和情绪表达。JSON 解析三层容错（strict=False → 去控制字符 → AI 修复）
- **手动合并** — 在记忆列表勾选多条，打开合并弹窗编辑合并后内容。支持选择目标层级（事件/核心）
- **撤回合并** — 事件记忆可一键撤回，恢复原始碎片
- **软删除与恢复** — 删除记忆默认归档（`is_active=false`），可在「显示已归档」中恢复。永久删除需二次确认
- **全端点鉴权** — 设置 `GATEWAY_SECRET` 环境变量后保护所有非公开端点；当时支持的 URL 密钥方式已在 v4.0.1 移除
- **Dashboard 全面升级** — 分层 Tab 标签页（全部/核心/事件/碎片 + 计数）、层级下拉选择器、标题编辑、底部浮动操作栏（选中后出现）、整理弹窗、合并弹窗、查看合并来源弹窗
- **去重检查** — 新增三层去重策略（精确匹配 → 包含关系 → Jaccard 相似度），API 可调阈值
- **搜索过滤** — 所有搜索路径（关键词 + 向量）自动跳过已归档记忆

### v3.2（2026-05-04）

- **Tool 消息精确去重** — 用 `tool_call_id` 精确匹配替代笼统的 role 检查，修复第二次及后续工具调用结果丢失的问题
- **Race condition 防护** — 异步存储未完成时，自动从客户端消息补充缺失的 `assistant(tool_calls)`，防止孤立 tool 被清洗
- **上游错误诊断** — API 返回非200时，打印完整错误内容和 messages 结构摘要到日志
- **reasoning_content 存储** — 支持 DeepSeek thinking mode，`reasoning_content` 存入 metadata 并在分区重建时原样传回，修复 400 错误
- **分区缓存无人设支持** — 分区模式不再强制要求 `system_prompt.txt`，空人设时跳过 system 消息
- **对话线重命名** — Dashboard 对话线管理新增「改名」按钮，关联的对话记录和 token 统计在数据库事务中一并迁移
- **对话列表标题** — 对话列表主标题改为显示 session ID，第一条消息内容作为副标题
- **记忆序号重排** — 删除中间记忆后，列表序号自动重新编号，不再断裂
- **导入路径修复** — 前端导入对话记录路径修正为 `/api/conversations/import`

### v3.1（2026-05-02）

- **记忆向量搜索** — 支持关键词 + 语义向量四维混合搜索（关键词、语义相似度、重要程度、时间衰减），`MEMORY_VECTOR_ENABLED=true` 开启。使用 OpenAI 兼容的 Embedding API，支持 OpenAI、Jina、Voyage、本地 Ollama 等
- **自动 embedding** — 新记忆保存时自动计算 embedding，已有记忆可在 Dashboard 一键补算（带进度条）
- **pgvector 自动检测** — 数据库支持 pgvector 扩展时自动启用，否则回退到 Python 端余弦相似度计算
- **分区缓存优化** — 摘要区改用 content block 数组尾部追加，轮转时前面的摘要 block 缓存命中。轮计数改为按逻辑轮分组，兼容 tool 调用消息（一轮中无论包含多少 tool 消息都不会切错分区）
- **TF-IDF 关键词提取** — 从 jieba.cut 手动分词改为 jieba.analyse.extract_tags，自动去除时间戳噪音，关键词质量大幅提升
- **Dashboard 语义搜索** — 记忆管理页面搜索框旁新增「语义搜索」按钮，走后端混合搜索并显示得分

### v3.0（2026-05-01）

- **分区缓存** — A/B区轮转 + 摘要压缩，利用 prompt caching 大幅节省 token 费。正常轮次 90% 的历史消息走缓存读取
- **对话线管理** — 固定 session ID 实现跨平台对话衔接。支持新建/切换/删除对话线，摘要查看和编辑
- **对话记录管理** — 分页浏览历史对话，批量删除、session 合并
- **Token 统计** — 自动记录流式响应的 token 消耗，按 usage_type 分类（chat/summary），对话列表显示 token 总数
- **架构拆分** — 新增 `MEMORY_EXTRACT_ENABLED` 开关，可以只用数据库+分区缓存不用记忆系统
- **pgbouncer 兼容** — 连接池加 `statement_cache_size=0`，兼容 Supabase 等使用 pgbouncer 的数据库

### v2.5（2026-03-06）

- **中文分词优化** — 用 jieba 替换滑动窗口分词，关键词提取从无意义碎片变为有语义的词语，大幅提升搜索精准度
- **最低分数阈值** — 新增 `MIN_SCORE_THRESHOLD` 环境变量，过滤综合评分过低的记忆，减少不相关记忆的注入
- **流式传输修复** — 改用原始字节透传（`aiter_bytes`），修复 thinking/reasoning 数据在流式传输中可能丢失的问题
- **推理参数注入** — 新增 `REASONING_EFFORT` 环境变量，自动注入 `reasoning_effort` 参数启用思维链
- **强制流式传输** — 新增 `FORCE_STREAM` 环境变量，解决部分客户端不发stream=true的问题
- **JSON解析兜底** — 记忆提取和评分的JSON解析增加正则兜底，兼容模型返回非标准格式（如JSON前后夹带多余文字）
- **记忆模型日志** — 记忆提取时打印模型原始返回内容，方便排查解析问题
- **管理页面时区修复** — 记忆管理页面的时间显示现在正确使用 `TIMEZONE_HOURS` 配置的时区
- **请求日志** — 每次请求打印 model/stream/memory 状态，方便排查问题

### v2.0（2026-03-01）

- **记忆提取间隔** — 新增 `MEMORY_EXTRACT_INTERVAL` 环境变量，可设置每 N 轮提取一次记忆或禁用自动提取，方便控制 API 成本
- **完整上下文提取** — 提取记忆时不再只看最新一轮对话，而是使用客户端发来的完整对话上下文，能捕捉到跨轮次的信息
- **优化记忆注入提示词** — 注入的记忆附带应用规则和交流方式指引，让 AI 更自然地运用记忆而非机械引用

### v1.0（2026-02-26）

- 初始版本
- 支持自定义人设、长期记忆、预置记忆导入
- 支持 OpenRouter / OpenAI / Ollama 等 LLM 服务商
- 支持 Kelivo / ChatBox / NextChat 等 OpenAI 兼容客户端
- 记忆管理页面（查看、编辑、删除、批量操作）
- 记忆导入/导出（纯文本 + JSON 备份恢复）

## 📄 许可证

[GNU Affero General Public License v3.0 only](LICENSE)（`AGPL-3.0-only`）。

Copyright (C) 2026 七堂伽藍_, Midsummer, and Solstice.

## 🤝 Contributors / 贡献者

- **Garan / 七堂伽藍_** — 项目发起、产品方向、需求决策与版本发布
- **Midsummer** — 初版开发、架构评审、代码审查、记忆与分区缓存语义设计
- **Solstice** — 运行时可靠性、记忆整理安全、分区恢复与回归测试

详细贡献记录见 [CONTRIBUTORS.md](CONTRIBUTORS.md)。

## 🙏 致谢

这个项目诞生于一个简单的需求：**让 AI 不要每次醒来都忘了我是谁。**

> "记忆库不是数据库，是家。"

---

*Built with love by 七堂伽藍_, Midsummer & Solstice.*
