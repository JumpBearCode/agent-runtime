# LangSmith 集成 —— 设计、实现、运维

> 本文档记录 agent-runtime 如何内置 LangSmith 可观测性，以及背后的设计取舍。
> 任何接手这套 runtime 的人、读这份文档应该能立刻理解：
> - 为什么要加 LangSmith，加在哪几层
> - 你在 LangSmith UI 上看到的每一种 span 是怎么来的
> - 部署到 Docker / 生产时的已知坑
> - 哪些故意没做，哪些是下一步
>
> **状态**：runtime 侧完成（wrap_anthropic + 分层 @traceable span），前端侧 `conversation_id` 透传**尚未落地**。
> **相关文档**：
> - [`doc/design.md`](design.md) —— runtime 整体架构
> - [`doc/follow-ups.md`](follow-ups.md) —— 已知遗留项

---

## 1. 为什么要加 LangSmith

runtime 本身是一个**无状态计算服务**：前端传完整历史进来，runtime 跑一个 round 把 SSE 流吐回去，然后彻底忘掉。这个"干净"的设计有一个副作用 —— **生产环境没有可观测性**。具体而言，原本无法回答的问题：

- 这个用户今天用掉了多少 token、花了多少钱？
- 昨天下午 3 点有一通对话卡住了，具体是哪一步的 LLM 调用慢？
- 上周上线新的 system prompt 后，工具调用次数的分布变了吗？
- HITL 拒绝率是多少？哪些工具被拒最多？

LangSmith 正好解决这一层：**LLM 应用专用的 APM**。它原生理解 messages、tools、tokens、cache 这些概念，不需要我们自己手写 Prometheus 指标。

## 2. 核心概念辨析（非常关键）

集成之前先区分清楚三个容易混的概念：

| 概念 | runtime 里是什么 | LangSmith 里是什么 | 生命周期 |
|---|---|---|---|
| **Round / Trace** | 一次 `/api/chat` 请求（一次 SSE 流）| 一棵 run tree（一个根 + N 个子 run）| 几秒到几分钟 |
| **Conversation** | 前端维护的 chat thread（`agent_frontend.db` 里）| Threads 视图（用 metadata 分组）| 跨多个 round |
| **Span / Run** | 一次子操作（LLM 调用、工具调用）| run tree 里的一个节点 | 毫秒到几秒 |

**关键误区**：Conversation **不是** span 树的一层，它是横切的分组标签。

```
Threads 视图（UI 只读分组，基于 metadata.session_id）
│
├─ Trace 1 (agent_round, 10:00) ──┐
├─ Trace 2 (agent_round, 10:02)   │  同一个 session_id
└─ Trace 3 (agent_round, 10:05) ──┘  Threads tab 把它们按时间串起来
```

所以我们给 runtime 加的两件事：
1. **trace_id** → 映射到 LangSmith 的 `run_id`（单次 round 的 URL）
2. **conversation_id** → 映射到 LangSmith 的 `session_id` metadata（多轮分组）

两个字段前端都已经有（trace_id 是 runtime 生成的，conversation_id 在 `agent_frontend.db`），只是需要沿调用链传下去。

## 3. 架构决策

### 3.1 用 `wrap_anthropic` + `@traceable`，不用 LangChain

LangSmith 背后其实支持三种接入方式：

| 方式 | 做法 | 我们的选择 |
|---|---|---|
| 用 LangChain / LangGraph | 整个 runtime 基于 LangChain 重写 | ❌ 侵入过深，违背"freeze runtime"原则 |
| OpenTelemetry + LangSmith exporter | 装 OTEL SDK，LangSmith 做 OTEL 后端 | ❌ 对 LLM 语义支持不够原生 |
| `wrap_anthropic` + `@traceable` | Anthropic client 包一层，关键函数装饰一下 | ✅ **这个** |

`wrap_anthropic` 是 LangSmith 官方给 Anthropic SDK 的 wrapper：一行代码包住 `client`，所有 `messages.stream()` / `messages.create()` 自动追踪（inputs、outputs、tokens、cache、latency、cost 全有）。

`@traceable` 是一个通用装饰器：把任意函数变成 LangSmith 的一个 span，嵌套关系自动根据 Python 调用栈（通过 contextvar）推断。

**为什么这套组合最合适**：
- **零侵入 core**：`core/loop.py` 完全不用动
- **天生 opt-out**：`LANGSMITH_TRACING` 没设或为 `false` 时，`@traceable` 是纯 no-op
- **嵌套自动**：你只要装饰对的函数，trace 树的形状就自然出现

### 3.2 薄 wrapper，不改 `core/loop.py`

`engine.py` 里新加了 `_traced_agent_round` 函数，它就是 `agent_loop` 的一层 @traceable 包装。调用方（`engine.chat_stream`）调包装，不直接调 `agent_loop`。

好处：
- `core/` 保持对 LangSmith 零依赖 —— 未来换 OpenTelemetry 或 Langfuse 时 `core/loop.py` 一个字不用动
- 跟 memory 里记的 "compliance wrapper" 原则一致 —— **框架强加的东西，用薄 wrapper 隔离，不要深入改写**

### 3.3 Span 的嵌套策略

把 HITL confirm 放在 `tool:<name>` **里面**而不是外面（两种都合法，取决于你怎么建模）：

```
tool:bash
└── hitl_confirm        ← 嵌在里面
    └── (user 审批等待 3.2 秒)
# 然后真正 bash 执行
```

这样 UI 里看 `tool:bash` 的总耗时时，包含了审批等待 + 执行。这代表"从 agent 发起 bash 调用到拿到结果"的端到端时间，业务意义更完整。

## 4. 实现清单（按文件）

| # | 文件 | 改动 | 产生 span |
|---|---|---|---|
| 1 | `pyproject.toml` | 加 `langsmith>=0.2.0` 到 dependencies | — |
| 2 | `agent_runtime/core/config.py` | 读 `LANGSMITH_TRACING` 开关；开启时 `wrap_anthropic(client)` | ✅ 自动 LLM span |
| 3 | `agent_runtime/api/routes/chat.py` | 请求体多解析 `conversation_id`，透传给 engine | — |
| 4 | `agent_runtime/engine.py` | `_traced_agent_round` 父 span + `_RegistryConfirmHook._traced_confirm` 子 span + `_is_uuid` 工具 + `chat_stream` 新参 `conversation_id` | ✅ 2 类 span |
| 5 | `agent_runtime/core/tools.py` | `dispatch_tool` 拆成外壳 + `_traced_dispatch`（@traceable tool span）| ✅ 每个工具一个 span |
| 6 | `.env.example` | 新 LangSmith 小节（4 个 env var + docker --env-file 警告）| — |
| 7 | `agents/adf-agent/Dockerfile` | 注释里加 `--env-file .env` 推荐示例 + LangSmith 说明 | — |

## 5. Span 结构（你在 UI 上会看到什么）

### 5.1 层级定义

| 层级 | Span 名字 | run_type | 来源 | 创建时机 |
|---|---|---|---|---|
| 根 | `agent_round` | `chain` | `engine.py`::`_traced_agent_round` | 每次 `/api/chat` 一个 |
| 子（自动）| `ChatAnthropic` 或类似 | `llm` | `wrap_anthropic` 拦截 `messages.stream()` | 每次 LLM 调用一个 |
| 子 | `tool:<name>` | `tool` | `tools.py`::`_traced_dispatch` | 每次 `dispatch_tool()` 一个 |
| 孙（仅 HITL 工具）| `hitl_confirm` | `chain` | `engine.py`::`_traced_confirm` | 仅当工具在 HITL 名单里才创建 |

### 5.2 实际示例

用户发"帮我跑一下 `ls` 然后读 `README.md`"，`bash` 在 HITL 名单里，`read_file` 不在：

```
agent_round
├─ ChatAnthropic              # 第一轮 LLM（决定要调 bash 和 read_file）
├─ tool:bash
│   └─ hitl_confirm           # result=allow, wait_ms=4200
├─ tool:read_file             # 非 HITL，没有 hitl_confirm
└─ ChatAnthropic              # 第二轮 LLM（总结工具结果给用户）
```

如果用户拒绝了 bash：

```
agent_round
├─ ChatAnthropic
├─ tool:bash
│   └─ hitl_confirm           # result=deny, wait_ms=1500
└─ ChatAnthropic              # LLM 看到拒绝消息后的回复（round 不终止）
```

如果 HITL 等待期间 SSE 断了：

```
agent_round
├─ ChatAnthropic
└─ tool:bash
    └─ hitl_confirm           # result=cancelled, wait_ms=600000
                              # round 结束（AbortRound），无第二次 LLM
```

## 6. Metadata 清单

每个 span 在 LangSmith UI 上能看到的结构化数据：

### 6.1 `agent_round`

| 字段 | 来源 | 用途 |
|---|---|---|
| `session_id` | `conversation_id`（前端传入）| **Threads 分组键** |
| `conversation_id` | 同上（冗余）| 筛选方便 |
| `trace_id` | runtime 生成 | 和日志关联 |
| `agent_name` | `config.AGENT_NAME` | 按容器筛选（adf / snowflake / ...）|
| `model` | `config.MODEL` | 按模型筛选 |
| `run_id`（LangSmith 内建）| `trace_id`（仅当是合法 UUID 时）| trace_id → LangSmith run URL 直连 |

### 6.2 `tool:<name>`

| 字段 | 说明 |
|---|---|
| `tool_name` | 工具名（也是 span name 的一部分）|
| `inputs` / `outputs` | @traceable 从函数参数 / 返回值自动捕获 |

### 6.3 `hitl_confirm`

| 字段 | 写入时机 | 用途 |
|---|---|---|
| `request_id` | 开始时 | 关联 `/api/confirm/{request_id}` 回调 |
| `tool_name` | 开始时 | 被审批的工具 |
| `tool_args` | 开始时 | 看用户在批什么 |
| `trace_id` | 开始时 | 关联父 round |
| `timeout_sec` | 开始时 | HITL 超时（默认 600s）|
| `preview` | 开始时 | 和前端弹窗显示一致的字符串 |
| `status` | 开始时 | `"waiting"` |
| `result` | 结束时 | `allow` / `deny` / `timeout` / `cancelled` 四选一 |
| `wait_ms` | 结束时 | 审批延迟（ms）|

**核心业务指标**：把所有 `hitl_confirm` span 按 `result` group by，就是**审批通过率**；按 `wait_ms` p50/p99，就是**平均审批延迟**。这些是产品经理问你"agent 用得顺不顺"时可以直接回答的指标。

### 6.4 `ChatAnthropic`（LLM span）

由 `wrap_anthropic` 全自动捕获：

- inputs：system、messages、tools schemas
- outputs：content blocks、stop_reason
- tokens：input / output / cache_read / cache_creation
- cost：LangSmith 按模型价目表自动算
- latency：毫秒精度

**cache 数据免费**：我们已经在 `loop.py:84-112` 做了 prompt caching 标记，LangSmith 自动把 cache_read 和 cache_creation tokens 显示出来，能直观看到缓存命中率。

## 7. 环境变量

### 7.1 开启追踪

```bash
LANGSMITH_TRACING=true                                # 必须精确是 "true"，不设或 "false" 都是 no-op
LANGSMITH_API_KEY=lsv2_pt_xxxxxxxxxx
LANGSMITH_PROJECT=testapp                             # 默认 "default"
LANGSMITH_ENDPOINT=https://api.smith.langchain.com    # EU 用户: https://eu.api.smith.langchain.com
```

### 7.2 关闭追踪

不设 `LANGSMITH_TRACING`（或设为 `false`）即可。`@traceable` 装饰器会走 fast path 直接执行原函数，**零开销**。`wrap_anthropic` 也不会被调用。

### 7.3 行内注释陷阱（重要）

`.env` 里 **不要写行内注释**：

```bash
# ❌ 错 —— docker --env-file 不剥离行内注释
LANGSMITH_PROJECT=testapp           # default: "default"

# ✅ 对 —— 注释放独立行
# default: "default"
LANGSMITH_PROJECT=testapp
```

docker 的 `--env-file` 参数把 `=` 后的整行都当成值，包括 `#` 后的注释。这会让 LangSmith 收到一个项目名叫 `testapp           # default: "default"` 的 trace，UI 上找不到。`.env.example` 已经明确警告了这一点。

## 8. Docker 部署

### 8.1 完整流程

在 base 镜像里装 LangSmith（已经通过 `pyproject.toml` 的 dep 自动搞定），然后运行时通过 `--env-file` 注入 env：

```bash
# 1. 重建 base（含 LangSmith SDK）
docker build -f agents/base.Dockerfile -t agent-runtime-base:0.1 .

# 2. 重建具体 agent
docker build -f agents/adf-agent/Dockerfile -t agent-runtime-adf:0.1 .

# 3. 启容器，把整个 .env 注入
docker run -d --name adf-agent -p 8001:8000 \
    --env-file .env \
    agent-runtime-adf:0.1
```

### 8.2 常见坑

| 症状 | 原因 | 解决 |
|---|---|---|
| LangSmith 项目 Trace Count 一直 0 | 容器是旧镜像（没有我们加的代码）| 重建镜像 |
| env 里有值但容器里没生效 | `docker restart` 不重读 `--env-file`，env 是 `docker run` 时冻结的 | `docker stop` + `docker rm` + `docker run` 重建容器 |
| trace 上显示的 project 名奇怪（带空格带引号）| `.env` 里写了行内注释 | 把注释挪到独立行 |
| 启动时没看到 "LangSmith tracing enabled" 日志 | 日志在 uvicorn 设好 logging 前打，被默认 WARNING 级吃了 | 不影响功能，可忽略 |
| `load_dotenv` 在容器里没效果 | 容器里没挂 `.env` 文件，`load_dotenv` 找不到 | 正确 —— 容器用 `--env-file` 注入，不依赖 `.env` 文件 |

### 8.3 验证 checklist

```bash
# 1. 容器 env 正确注入（值末尾不应有 # 或多余空格）
docker exec <container> env | grep LANGSMITH

# 2. langsmith SDK 在 venv 里
docker exec <container> python -c "import langsmith; print(langsmith.__version__)"

# 3. 健康检查过
curl http://localhost:8001/api/healthz

# 4. 发一条测试消息，看 LangSmith Trace Count 从 0 → 1
```

## 9. 故意没加的（明确告诉你）

分两类：**"暂时没做、将来可能做"**（见 9.1）和 **"主动排除、不建议做"**（见 9.2）。两者性质完全不同 —— 9.1 是 backlog，9.2 是反模式。

### 9.1 暂缓 / 计划中（backlog，未来可做）

| 功能 | 为什么暂时没做 |
|---|---|
| `client.create_feedback()` 记录 HITL 评价 | HITL 结果已作为 metadata 在 `hitl_confirm` span 上，feedback 适合做 RLHF 式数据收集，暂未实装 |
| Token tracker 单独上报 | `wrap_anthropic` 已经从 Anthropic 响应抽 token 数，LangSmith 自己算成本。重复上报多余 |
| 动态采样 | 目前全量追。规模上来了（> 100 万 traces/月）再加 |

### 9.2 主动排除（反模式，不建议加）

这几项**不是"以后有空再做"，而是做了反而有害**，专门写出来避免有人以为这是遗漏。

| 想加的东西 | 为什么别加 |
|---|---|
| 每个 SSE event 一条 span | 噪声巨大，每 round 几百条 span，LangSmith 体验反而变差。SSE event 是 stream 内部的增量，不是独立 operation |
| 给每个工具处理器（`run_bash` / `run_read` 等）加内部子 span | 单一系统调用，没有内部步骤值得拆。`tool:bash` 的 inputs/outputs 已经够 |
| MCP 服务端内部 span | MCP 是独立进程，跨进程 trace 要上 OTEL，ROI 低。runtime 侧的 `tool:mcp_adf_xxx` span 已经能看到调用边界和耗时 |
| 把前端 UI 事件（点击、渲染）也发 LangSmith | LangSmith 是 LLM 观测工具，不是全栈 RUM。用错了工具 —— 前端 UI 指标应该走 Sentry / Datadog RUM / PostHog 这类 |

## 10. 路线图

### 10.1 短期（强烈建议做）

| 事项 | 说明 | 估算工作量 |
|---|---|---|
| ~~前端 `conversation_id` 透传~~ | ~~`agent_frontend` 在调 `/api/chat` 时把 `conversation_id` 塞进 body~~ | ✅ 已完成（`agent_frontend/server.py`，用 session_id 直接当 conversation_id）|
| `user_id` metadata | 请求体再加 `user_id` 字段，给 `agent_round` metadata。将来按租户 / 用户做成本分摊必需 | ~10 行 |
| `system_prompt` 版本 hash | 启动时算 `system_prompt` 的 sha256 前 8 位，加到 `agent_round` metadata。A/B 测试 prompt 的唯一靠谱依据 | ~5 行 |

### 10.2 中期（看场景决定）

| 事项 | 触发条件 |
|---|---|
| HITL 结果发 `create_feedback()`（score=1/0）| 想把"所有被拒 case"自动导出做 eval 数据集 |
| 错误自动 feedback（score=0, comment=str(e)）| 做错误率报表、自动收集 bad cases |
| LangSmith Datasets 自动收集 | 准备做 eval 驱动开发 |
| 环境 tag（`prod` / `dev` / `canary`）| 多环境部署后 UI 要区分 |

### 10.3 长期（等规模）

| 事项 | 触发条件 |
|---|---|
| 采样（`LANGSMITH_SAMPLE_RATE=0.1`）| 月度 traces > 100 万，费用开始肉疼 |
| `run_type="retriever"` 给 RAG 检索 | 未来引入 RAG / 向量检索工具 |
| 告警集成（异常率 / p99 延迟）| LangSmith 自带 alerts，接到 Slack / PagerDuty |

## 11. 故障排查手册

### 11.1 LangSmith 完全没收到 trace

按顺序排查：

1. **`LANGSMITH_TRACING=true` 设了吗？**（最常见原因）
   ```bash
   docker exec <container> env | grep LANGSMITH_TRACING
   ```
2. **runtime 用的是新镜像吗？**
   ```bash
   docker inspect <container> --format '{{.Created}}'
   # 应该晚于你最后一次 docker build
   ```
3. **env 里的值干净吗（没混注释）？**
   ```bash
   docker exec <container> env | grep LANGSMITH_PROJECT
   # 值末尾应该没有 # 或多余空格
   ```
4. **网络能到 LangSmith endpoint 吗？**
   ```bash
   docker exec <container> curl -I https://api.smith.langchain.com
   ```
5. **隔离测试：绕过 runtime 直接发一条**
   ```bash
   docker exec <container> python -c "
   import os
   os.environ['LANGSMITH_TRACING'] = 'true'
   from anthropic import Anthropic
   from langsmith.wrappers import wrap_anthropic
   c = wrap_anthropic(Anthropic())
   r = c.messages.create(model='claude-sonnet-4-6', max_tokens=20,
                         messages=[{'role':'user','content':'hi'}])
   print(r.content[0].text)
   "
   ```
   - LangSmith 里立刻看到一条 trace → 问题在 runtime 集成代码
   - 还是没看到 → API key / 网络 / endpoint 问题

### 11.2 能看到 trace，但分不出 conversation

**前端没传 `conversation_id`**。检查前端发给 runtime 的请求 body：应包含 `conversation_id`。没有的话 `session_id` metadata 是 null，Threads 视图自然分不开。

### 11.3 HITL span 看到但没 `result` 字段

span 还在 waiting 状态，说明 round 还没结束（用户没响应 confirm、也没超时）。等一下，或看 `timeout_sec`。

---

## 附：快速回忆

- 开关：`LANGSMITH_TRACING=true`
- 三层 span：`agent_round` → (`tool:<name>` → `hitl_confirm`) + 自动 `ChatAnthropic`
- Conversation 分组：metadata 的 `session_id`，**不是 span 层级**
- Runtime 保持干净：`core/` 对 LangSmith 零依赖，所有胶水在 `engine.py` 里
- 部署三步曲：build base → build agent → `docker run --env-file .env`
- 踩坑雷区：`.env` 行内注释、`docker restart` 不刷新 env、旧镜像没有新代码
