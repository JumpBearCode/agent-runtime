# Agent Runtime — 设计与架构

> 本文档记录了从 CLI 原型到 stateless FastAPI 容器的完整演进，以及当前系统的所有关键设计决策。
> 任何接手这个项目的人——包括未来的你自己、新加入的工程师，或一个新的 AI session——读完这一份文档应该能立刻理解：
> - 系统现在是什么样的
> - 为什么是这样的
> - 还差什么
> - 怎么往前推
>
> **状态**：基础架构完成，等待前端项目接入。后端跑得起来、能 stream、HITL 已通。
> **相关文档**：
> - [`doc/follow-ups.md`](follow-ups.md) — 已知遗留问题清单
> - [`doc/storage.md`](storage.md) — 前端 session 持久化（SQLite / Postgres）的 schema 与取舍

---

## 1. 项目使命

`agent-runtime` 是一个 **可插拔的、容器化的 AI agent 计算服务**。

核心定位：
- **不是 SaaS、不是平台**——它只是"一个会推理 + 调工具的进程"。
- **每个 agent = 同一个镜像 + 不同的 skills/MCP/system prompt 配置**。例：ADF agent、Fabric Lakehouse agent、Snowflake agent，全部用同一个 base image，差异在 build 时 COPY 进去。
- **完全无状态**——会话历史、用户身份、UI 都不归后端管。后端只接收 `messages: [...]`，返回 SSE 事件流。
- **目标用户**：企业内部数据工程师（用户的语境是 Snowflake/ADF 平台运维）。预期并发不超过几百个活跃 chat。

护城河在哪里：
- 不在 runtime 本身——runtime 是冻结的、可替换的。
- 在 **每个 agent 的 skills + MCP servers + system prompt**。这些是和具体业务深度耦合的领域知识。

---

## 2. 演进路径

### 起点（session 开始时）
项目是个三层结构：
- `agent_runtime/` — 同步的 agent 内核 + CLI 入口（`__main__.py`）
- `agent_frontend/` — 包含两个 frontend：CLI 和一个 FastAPI Web server，通过 `AgentEngine` 包裹 agent_runtime
- 配置散落在 `.agent_settings/`、`.agent_settings_example/`，CWD-walking 三层 settings 解析

主要痛点：
- FastAPI 在 frontend 包里，不在 runtime 里
- Engine 用 `ThreadPoolExecutor` + `threading.Event` 做 sync↔async 桥接
- 大量全局 mutable state（`tools_mod.HOOKS`、`tools_mod.TODO`）
- Session 由后端 own（写 jsonl 文件），跨容器无法共享

### 决策一：半套迁移而非全套异步化
讨论了"native async 重写"vs "FastAPI wrapper 包一层"两条路。
- 全套需要重写 `loop.py`、`tools.py`、`mcp_client.py`、`compression.py` 全部异步化，约 2-3 天。
- 半套保留同步内核，只在外面套 FastAPI + ThreadPoolExecutor，约 1-2 天。

**选择半套**，理由：
- 用户场景是企业内部（几十到几百用户），半套在单 worker 200 并发以内完全够用
- 横向扩 worker 比异步化便宜得多（`uvicorn --workers 4`）
- async-native 主要省机器钱，但用户还远没到那个量级
- 半套保留了未来升级到 native async 的路径——API contract 不变，内部重写

记录在 user 的 memory：「**For framework mandates, prefer thin wrapper/subclass over deep rewrite**」。

### 决策二：FastAPI 嵌入 agent_runtime
原方案：FastAPI 留在 `agent_frontend/`。
新方案：把 FastAPI 搬进 `agent_runtime/api/`，删除 `agent_frontend/cli/`，runtime 自己是个完整 service。

理由：
- 容器哲学："这个 container 里应该要有这个 agent 的所有东西"——运行时、HTTP 接口、skills、settings、prompt 全部 baked in
- `agent_frontend/` 暂时保留为 placeholder，因为用户准备完全重写前端

### 决策三：Stateless（核心哲学）
最初保留了 `SessionStore`（jsonl 持久化）和 `_SessionRegistry`（in-memory dict），后端 own 历史。

讨论后决策**完全无状态化**：
- 后端不再 own 任何 conversation 历史
- 每次 `POST /api/chat` 由前端贴上完整 `messages` 数组
- 删除 `core/session.py`、`api/routes/sessions.py`、所有 5 个 session API endpoints
- 历史持久化外包给前端 + Postgres（chat 表 + conversation 表）

理由：
- 容器可以随便重启、横向扩，state 不会丢
- 不同 agent container 之间不需要互通 session
- 前端切换 agent 时，由前端决定要不要带历史过去（不同 agent 历史互不污染是更自然的默认）

### 决策四：Per-thread / Per-chat state
重构后发现一系列"上一个状态时代留下的 cross-chat 污染" bug：
1. `tools_mod.TODO` 是全局——chat A 写的 todo，chat B 能读到
2. `self.tracker` 是 engine 级——所有 chat 的 token 累加到一起
3. `self._on_event` 是 engine instance attr——并发 chat 互相覆盖，HITL `confirm_request` 路由到错误的 SSE 流（最严重）
4. `auto_compact` 写 transcript 到磁盘——和无状态理念矛盾

修法：
- TODO + HookManager → `threading.local()`（per-agent-thread）
- TokenTracker → 每次 `chat_stream` 新建（per-request）
- `_on_event` → 直接传给 `_RegistryConfirmHook` 构造器（per-trace 闭包）
- `compression.py` 整个删掉（用户说"以后再想 solution"）

### 决策五：Per-agent uv project
最初每个 agent Dockerfile 硬编码 `pip install azure-identity ...`——版本浮动、不可复现。
改成每个 agent 自己有 `pyproject.toml` + `uv.lock`，Dockerfile 通过 `uv export → pip install -r` 安装。

### 决策六：观测性整顿
- `core/loop.py` 全部 `print()` + ANSI escape 替换为 `logger.info/debug`
- `api/app.py` 配置 `logging.basicConfig` 结构化前缀（时间戳 + level + 模块 + 消息）
- `LOG_LEVEL` env var 可调

---

## 3. 核心设计哲学：Stateless

### 责任划分

```
┌──────────┐  POST /api/chat                ┌─────────────┐
│ Frontend │  body: {messages: [...全量...],│Agent Runtime│
│   (你)    │         trace_id: "..."}      │ (stateless) │
│          │ ─────────────────────────────▶│             │
│          │ ◀──────── SSE stream ─────────│             │
│          │  events: text_delta, tool_*,  │             │
│          │          done(reason)         │             │
└────┬─────┘                                └─────────────┘
     │ on `done`：把这一轮 user + assistant message
     │ 结构化后写入 Postgres
     ▼
┌──────────────────────────┐
│ Postgres (前端 own)       │
│  chat (id, agent, ...)   │
│  conversation (chat_id,  │
│      role, content_json, │
│      ts, ...)            │
└──────────────────────────┘
```

### 后端不持有的东西
- 用户身份 / 会话 id / 任何 user-level 状态
- Conversation 历史（除了一个 round 内的临时 in-memory list）
- 任何会随 container 重启丢失而不能从前端重建的东西

### 后端持有的东西（per-process / per-worker）
- `AgentEngine` 单例：`SkillLoader`、`MCPManager`、`_ConfirmRegistry`、`ThreadPoolExecutor`
- 这些是**计算资源**，不是用户数据

### 后端持有的东西（per-chat / per-request）
- 一个 `Todo` 实例（thread-local）
- 一个 `TokenTracker` 实例
- 一个 `HookManager` 实例（thread-local，里面装着 trace 绑定的 confirm hook）
- chat 结束就丢

### 这意味着什么
- 容器可以随便横向扩——任何 worker 都能服务任何请求
- 容器可以随便 kill / 重启——丢失的只是 in-flight 的 SSE 流，前端重连重发即可
- 不同 agent container 之间无需通信
- 前端切 agent → 前端决定带不带历史
- 后端无 DB 依赖，无外挂 volume 依赖，docker pull && docker run 即可

### 这意味着什么不能做
- 用户离开 24 小时回来"接着上次的对话"——这是前端 + Postgres 的责任，不是后端的
- 后端没有审计日志（暂时；如果需要可以加 transcript-only sink，详见 follow-ups）
- 后端不知道"同一个用户"——每个请求都是孤立的

---

## 4. 系统全景

```
┌──────────────────────────────────────────────────────────────────┐
│   Frontend Project (你下一个 session 要写的)                      │
│                                                                  │
│   ┌──────────────┐                                               │
│   │ Agent picker │  [ADF agent ▼]  [Fabric] [Snowflake] [...]    │
│   └──────┬───────┘                                               │
│          │ 调 GET /api/info on each container, container 自报家门│
│   ┌──────▼─────────────────────────────────────────┐             │
│   │ Chat window (per active chat)                  │             │
│   │  • 从 Postgres 读 conversation 历史            │             │
│   │  • 渲染 SSE 事件流                             │             │
│   │  • HITL 模态框：'Allow tool X? [Yes/No]'        │             │
│   │    POST /api/confirm/{request_id}              │             │
│   └────────────────┬───────────────────────────────┘             │
│                    │                                             │
│   ┌────────────────▼───────────────────┐                         │
│   │ Postgres (前端 manage)              │                         │
│   │   chat:         id, agent, title... │                         │
│   │   conversation: chat_id, role,      │                         │
│   │                 content_json, ts    │                         │
│   └────────────────────────────────────┘                         │
└────────────────┬─────────────────────────────────────────────────┘
                 │ HTTP / SSE
        ┌────────┼─────────────┬──────────────────┐
        │        │             │                  │
        ▼        ▼             ▼                  ▼
   ┌────────┐ ┌─────────┐ ┌──────────┐      ┌──────────┐
   │  ADF   │ │ Fabric  │ │Snowflake │      │  Custom  │
   │ runtime│ │ runtime │ │ runtime  │  ... │ runtime  │
   │  :8001 │ │  :8002  │ │  :8003   │      │  :8004   │
   └───┬────┘ └────┬────┘ └────┬─────┘      └────┬─────┘
       │           │           │                 │
       │     全部用 agent-runtime-base 镜像        │
       │     差异在 baked-in skills/MCP/prompt   │
       ▼                                         ▼
   /app/skills/{adf-*}              /app/skills/{custom-*}
   /app/settings/mcp.json           /app/settings/mcp.json
   /app/prompts/system.md           /app/prompts/system.md
```

**关键性质**：
- 所有 runtime 容器是**同一个镜像家族**（base + per-agent overlay）
- 一个新 agent = 一个新 Dockerfile + 一个新 container 部署
- 前端通过 `GET /api/info` 让每个容器自报家门，不要在前端硬编码 agent 列表

---

## 5. 单容器内部架构

```
┌─────────────────────────────────────────────────────────────────┐
│   Container: e.g. agent-runtime-adf:0.1                          │
│                                                                  │
│   ┌──────────────────────────────────────────────────────────┐  │
│   │  uvicorn (1+ workers)                                    │  │
│   │  ┌────────────────────────────────────────────────────┐  │  │
│   │  │  FastAPI app (agent_runtime/api/app.py)            │  │  │
│   │  │   • lifespan → 构造/销毁 AgentEngine               │  │  │
│   │  │   • CORS middleware (env: AGENT_CORS_ORIGINS)      │  │  │
│   │  │   • routes/{meta,chat,confirm}.py                  │  │  │
│   │  └────────────────────┬───────────────────────────────┘  │  │
│   │                       │ async                            │  │
│   │  ┌────────────────────▼───────────────────────────────┐  │  │
│   │  │  AgentEngine (agent_runtime/engine.py)              │  │  │
│   │  │   • SkillLoader, MCPManager        (单例，read-only)│  │  │
│   │  │   • _ConfirmRegistry               (per-engine)     │  │  │
│   │  │   • ThreadPoolExecutor(max_workers=64)              │  │  │
│   │  │   • chat_stream(messages, trace_id)                 │  │  │
│   │  │     → 每次新建 Todo + Tracker + HookManager         │  │  │
│   │  │     → run agent_loop in thread                      │  │  │
│   │  │     → yield events from queue                       │  │  │
│   │  └────────────────────┬───────────────────────────────┘  │  │
│   │                       │ run_in_executor                  │  │
│   │  ┌────────────────────▼───────────────────────────────┐  │  │
│   │  │  agent_loop (agent_runtime/core/loop.py) — sync     │  │  │
│   │  │   ↓ Anthropic SDK (sync messages.stream)            │  │  │
│   │  │   ↓ tools.dispatch_tool(name, args)                 │  │  │
│   │  │       • bash / file IO  (subprocess, sync)          │  │  │
│   │  │       • MCP call → bridge to bg event-loop thread   │  │  │
│   │  │       • HITL hook → block on per-request Event      │  │  │
│   │  └─────────────────────────────────────────────────────┘  │  │
│   └──────────────────────────────────────────────────────────┘  │
│                                                                  │
│   Baked into image at /app:                                      │
│     /app/agent_runtime/   ← 共享代码                             │
│     /app/skills/          ← 这个 agent 的 skills                 │
│     /app/settings/        ← mcp.json + HITL.json                 │
│     /app/prompts/         ← system.md                            │
│     /app/mcp/             ← (optional) 这个 agent 的 MCP server  │
│                                                                  │
│   Env vars:                                                      │
│     AGENT_NAME, MODEL_ID, ANTHROPIC_API_KEY,                     │
│     AGENT_HITL_TIMEOUT (default 600),                            │
│     AGENT_MAX_CONCURRENT_CHATS (default 64),                     │
│     LOG_LEVEL (default INFO),                                    │
│     AGENT_CORS_ORIGINS (default *)                               │
└──────────────────────────────────────────────────────────────────┘
```

---

## 6. 并发模型

### 一个请求 = 一个线程

```
POST /api/sessions/.../chat               ← FastAPI 异步主循环 accept
     │
     ▼
chat_stream(messages, trace_id)           ← async generator
     │
     ├─ 新建 Todo, Tracker, HookManager (per-chat)
     ├─ asyncio.Queue(maxsize=1024)
     ├─ run_in_executor(ThreadPoolExecutor, _run_sync)
     │
     ▼
线程 #N 被借出
     │
     ├─ tools_mod.set_thread_hooks(hooks)   ← thread-local 绑定
     ├─ tools_mod.set_thread_todo(todo)
     │
     ├─ agent_loop(messages, system, tracker, on_event=on_event)
     │   ├─ LLM stream (round 1)            │
     │   ├─ tool: bash "ls"                  │ 全部
     │   ├─ tool: read_file "..."            │ 在
     │   ├─ LLM stream (round 2)             │ 同一个
     │   ├─ tool: mcp_adf_xxx                │ 线程
     │   ├─ LLM stream (round 3, done)       │
     │   │                                   │
     │   每个步骤通过 on_event(raw) 推事件    │
     │   on_event 用 loop.call_soon_threadsafe →
     │   构造 dataclass → put 到 asyncio.Queue
     │
     ▼
线程 #N 归还 pool（agent_loop return）
     │
     ▼
SSE 端 yield queue.get() → 收到 None sentinel → 关 SSE
```

**核心性质**：
- FastAPI 主循环**永远不 block**——只在 `await queue.get()`
- 每个 chat 线程**完全独立**——只通过自己的 queue 与外界交互
- Tool 执行是**串行**的（同一线程内）
- 多个 chat 是**完全并行**的（不同线程）

### Thread-local 隔离

`agent_runtime/core/tools.py` 维护 `_thread_state`：
- `_thread_state.hooks` — 这个线程的 HookManager（per-chat）
- `_thread_state.todo` — 这个线程的 Todo

任何工具 dispatch 都通过 `_active_hooks()` / `active_todo()` 读 thread-local，模块级 fallback 仅供测试用。

**为什么必须 thread-local 而不能 engine-instance**：因为多个 chat 共享同一个 engine 实例，instance attr 会被覆盖。这是 session 重构时连续踩了三个雷的根本原因（TODO、Tracker、_on_event 都是同一类问题）。

### MCP 共享后台 loop

`MCPManager` 起一个 daemon 线程跑 asyncio event loop。所有 chat 线程通过 `asyncio.run_coroutine_threadsafe` 调度 MCP 调用到这个 loop，再 `future.result(timeout=120)` 同步等待。

- IO-bound，多 chat 并发调 MCP 没问题（loop 内部 await 是 cooperative）
- 但每个调用方线程会 block 到结果回来——所以慢 MCP 会占住线程槽

### Concurrency hazards 已修

| Hazard | 原 bug | 修法 |
|---|---|---|
| `tools_mod.TODO` 全局 | chat A 写 todo，chat B 读到 | thread-local |
| `self.tracker` engine attr | 所有 chat 累加 token | per-chat 新建 |
| `self._on_event` engine attr | HITL 推到错误 SSE 流 | 闭包传给 hook |
| `tools_mod.HOOKS` 全局 | 类似 TODO | thread-local |

### 还没修的（详见 follow-ups.md）
- ThreadPoolExecutor 用非 daemon 线程，HITL 阻塞会导致 container 关闭挂起
- `asyncio.get_event_loop()` deprecated
- SSE queue 满会静默丢事件

---

## 7. HITL（Human-in-the-Loop）流程

### 数据结构

```python
# agent_runtime/engine.py
@dataclass
class ConfirmSlot:
    event:      threading.Event
    trace_id:   str
    tool_name:  str
    created_at: float
    result:     Optional[bool] = None  # True=allow, False=deny, None=timeout/cancel

class _ConfirmRegistry:
    _slots:     dict[str, ConfirmSlot]      # req_id → slot
    _by_trace:  dict[str, set[str]]          # trace_id → {req_ids}
    _lock:      threading.Lock
```

`req_id` 是 UUID hex。`trace_id` 是这次 chat 请求的标识，用于 SSE 断开时清理这条 trace 名下所有未完成的 confirms。

### 完整流程

```
T0   Chat A 的 agent_loop 调用 dispatch_tool("write_file", {...})
     │
     ├─ tool 在 HITL 名单里，hook 触发
     │  ├─ registry.open(trace_id="A", "write_file") → req_id, slot
     │  ├─ on_event({"type":"confirm_request", "request_id":req_id, ...})
     │  │  → SSE 推到 chat A 的前端
     │  └─ slot.event.wait(timeout=600)   ← 线程 block
     │
     │ ─── 三个可能分支 ───
     │
     ├─ 用户在 600s 内点 Allow
     │  │  POST /api/confirm/{req_id} {allowed:true}
     │  │  → registry.resolve(req_id, True)
     │  │  → slot.result = True
     │  │  → slot.event.set()
     │  └─ hook 收到 ALLOW → tool 真实执行
     │
     ├─ 用户在 600s 内点 Deny
     │  │  POST /api/confirm/{req_id} {allowed:false}
     │  │  → slot.result = False, event.set()
     │  └─ hook 收到 DENY → tool_result = "Blocked: User rejected"
     │     → agent_loop 继续，下一轮 LLM 看到拒绝消息可能换方案
     │
     ├─ 600s 超时
     │  │  slot.event.wait() 返回 False
     │  │  → registry.discard(req_id)
     │  │  → raise AbortRound("HITL timeout (600s)")
     │  └─ agent_loop 捕获 AbortRound：
     │     ├─ 给当前 tool_use 补一个 tool_result placeholder
     │     ├─ 给本轮所有 *剩余* 的 tool_use 也补 placeholder
     │     │  （Anthropic API 要求 tool_use 必须配 tool_result）
     │     ├─ messages.append({"role":"user","content":[...placeholders]})
     │     ├─ on_event({"type":"done","stop_reason":"hitl_timeout"})
     │     └─ return — 线程释放
     │     → 前端用户重新发消息时，history 是合法的，agent 会继续
     │
     └─ SSE 客户端断开（用户关浏览器）
        │  chat_stream 的 try 块抛 asyncio.CancelledError
        │  → registry.cancel_trace("A") → 该 trace 所有 slots 唤醒
        │  → slot.result 设为 None
        │  → hook 看 result is None → raise AbortRound("client disconnected")
        └─ 同 timeout 路径
```

### 关键性质

- **Per-request 隔离**：每个 confirm 有自己的 Event，并发 chats 不互相 block
- **History 永远合法**：不管走哪条路径，都补齐 tool_result，下次 LLM call 不会因 unmatched tool_use 报 400
- **Frontend 能恢复**：超时不是丢失工作，而是给 LLM 一个明确的"被拒绝/超时"信号 + 提示用户可以重发
- **路由正确**：`_RegistryConfirmHook` 在构造时就闭包了 `on_event` 和 `trace_id`，不读任何 engine instance attr——这是修复 `_on_event` 串台 bug 的关键

---

## 8. API 契约

7 个 endpoint，全部 stateless。

### Meta（前端 picker 用）

```
GET  /api/healthz
     → 200 {"status": "ok"}

GET  /api/info
     → 200 {
         "agent_name":   "adf",       // 容器自报家门
         "model":        "claude-sonnet-4-6",
         "mcp_tools":    ["mcp_adf_list_pipelines", ...],
         "hitl_tools":   ["bash", "write_file", "edit_file"],
         "hitl_timeout": 600
       }

GET  /api/tools
     → 200 ["bash", "read_file", "write_file", "edit_file",
            "todo_write", "todo_read", "load_skill",
            "mcp_adf_xxx", ...]

GET  /api/skills
     → 200 {
         "adf-overview":            "Get a comprehensive overview of...",
         "find-pipelines-by-service":"Find all pipelines that use..."
       }

GET  /api/skills/{name}
     → 200 {"name": "adf-overview", "content": "<full markdown body>"}
     → 404 {"detail": "unknown skill: ..."}
```

### Chat（核心）

```
POST /api/chat
     body: {
       "messages":  [...],            // 全量 history，最后一条必须是 role=user
       "trace_id":  "uuid-string"     // optional, 用于 HITL 路由 + SSE 断开清理
     }
     → 200 text/event-stream  (SSE)
     → 400 {"detail": "messages must be a non-empty array"}
     → 400 {"detail": "last message must have role=user"}
```

### HITL Confirm

```
POST /api/confirm/{request_id}
     body: {"allowed": bool}
     → 200 {"status": "ok", "allowed": <bool>}
     → 410 {"detail": "confirm request no longer pending"}
```

`410 Gone` 表示这个 request_id 已经超时、被取消、或从未存在。前端收到 410 应该把 modal 关掉、显示"timeout/已撤销"。

---

## 9. SSE 事件契约

每个事件用 `event: <type>\ndata: <json>\n\n` 编码（标准 SSE）。所有事件类型在 `agent_runtime/api/schemas.py` 定义。

### 事件类型

| Type | Fields | 含义 |
|---|---|---|
| `text_delta` | `{text: str}` | LLM 文本流增量 |
| `text_stop` | `{}` | 文本块结束 |
| `thinking_start` | `{}` | extended thinking 开始（如启用） |
| `thinking_delta` | `{text: str}` | thinking 流增量 |
| `thinking_stop` | `{}` | thinking 结束 |
| `tool_call` | `{id, name, args, args_summary}` | LLM 决定调一个工具 |
| `tool_result` | `{id, name, output, is_error}` | 工具执行返回 |
| `token_usage` | `{turn:{...}, total:{...}, cost:str}` | 这一 round 的 token 消耗 |
| `confirm_request` | `{request_id, tool_name, tool_args, preview}` | HITL 请用户批准 |
| `status` | `{message: str}` | 状态消息（很少用） |
| `done` | `{stop_reason: str}` | 流结束。`stop_reason` 见下 |
| `error` | `{message: str}` | 内部错误，流可能未完整 |

### `done.stop_reason` 取值

- `"end_turn"` — agent 自然结束（最常见）
- `"max_tokens"` — 触达 token 上限
- `"hitl_timeout"` — HITL 超时或 SSE 断开导致本轮 abort
- `"tool_use"` — agent 还想调工具但被截断（不应该出现，意味着 bug）

### 顺序保证

- `text_delta...text_stop` 必成对，可能多次出现（一轮里 LLM 可能输出多段文本）
- `thinking_start → thinking_delta* → thinking_stop` 是连续的块
- 每个 `tool_call` 对应**恰好一个** `tool_result`（包括 HITL 拒绝/超时的 placeholder）
- `confirm_request` 在 `tool_call` 之后、`tool_result` 之前
- `token_usage` 在每轮 LLM call 之后发出，可能在 `done` 之前
- `done` 终结流。**前端看到 `done` 后应当关闭 EventSource**
- `error` 可在任意位置打断流

### 前端如何重建 conversation

后端 mutate 自己内部的 messages list 但**不返回给前端**。前端必须从 SSE 事件流自己拼出 assistant message：

```
assistant_msg.content = [
    {type:"text", text: <所有 text_delta 拼起来，按 text_stop 分块>},
    {type:"thinking", thinking: <thinking_delta 拼起来>},  // 如启用
    {type:"tool_use", id, name, input: args},  // 来自 tool_call
    ...
]
```

然后跟着这些 assistant_msg 之后的 user message 是 tool_result（来自 SSE 的 `tool_result` 事件）。下次 chat 把这些一起 POST 回 backend。

---

## 10. 容器化模型

### 镜像层级

```
agent-runtime-base:0.1               (agents/base.Dockerfile)
   ↑ FROM
agent-runtime-adf:0.1                (agents/adf-agent/Dockerfile)
   ├─ pip install azure-* 等额外 deps
   ├─ COPY skills/ → /app/skills
   ├─ COPY settings/ → /app/settings
   ├─ COPY prompts/ → /app/prompts
   ├─ COPY mcp/ → /app/mcp
   └─ ENV AGENT_NAME=adf
```

### Build

```bash
# 在项目根
docker build -f agents/base.Dockerfile -t agent-runtime-base:0.1 .
docker build -f agents/adf-agent/Dockerfile -t agent-runtime-adf:0.1 .
```

### Run

```bash
docker run --rm -p 8001:8000 \
    -e MODEL_ID=claude-sonnet-4-6 \
    -e ANTHROPIC_API_KEY=... \
    -e ADF_SUBSCRIPTION_ID=... \
    -e ADF_RESOURCE_GROUP=... \
    -e ADF_FACTORY_NAME=... \
    agent-runtime-adf:0.1
```

### Env vars 全集

| Var | 默认 | 含义 |
|---|---|---|
| `MODEL_ID` | (required) | Anthropic model id |
| `ANTHROPIC_API_KEY` | (required) | API key |
| `ANTHROPIC_BASE_URL` | (optional) | 自建 proxy / Foundry endpoint |
| `AGENT_NAME` | `""` | 容器自报家门，前端 picker 显示用 |
| `AGENT_WORKDIR` | container 内 cwd | 工具文件 IO 根目录 |
| `AGENT_SETTINGS_DIR` | `$AGENT_WORKDIR/.agent_settings` | mcp.json + HITL.json |
| `AGENT_SKILLS_DIR` | `$AGENT_WORKDIR/skills` | SKILL.md 目录 |
| `AGENT_SYSTEM_PROMPT_FILE` | `$AGENT_WORKDIR/prompts/system.md` | 默认读这个文件 |
| `AGENT_HITL_TIMEOUT` | `600` | HITL 等待秒数 |
| `AGENT_MAX_CONCURRENT_CHATS` | `64` | ThreadPool max_workers |
| `TOOL_OUTPUT_LIMIT` | `10000` | tool_result 最大字符数 |
| `LOG_LEVEL` | `INFO` | logging 级别 |
| `AGENT_CORS_ORIGINS` | `*` | CORS 白名单（生产改） |

---

## 11. 前端规格（你下一个 session 要做的）

### 整体形态

一个 Web app（用什么栈你定，但建议 Next.js / Vite + React）。本地 dev 起在 :3000，生产部署到企业内网。

### 关键页面 / 组件

#### 11.1 Agent Picker（顶部菜单或左侧栏）

- 配置文件 / env 列出可用 runtime 容器的 base URL，例：
  ```
  [
    {url: "http://adf-runtime:8000", default: true},
    {url: "http://fabric-runtime:8000"},
    {url: "http://snowflake-runtime:8000"}
  ]
  ```
- 启动时对每个 URL 调 `GET /api/info`，拿到 `agent_name` + `model` + `hitl_tools` + `hitl_timeout` 渲染选项
- 健康检查：`GET /api/healthz`，挂掉的容器在选项里灰掉
- 选中一个 agent → 后续所有 chat 操作都打到这个 base URL

#### 11.2 Chat List（侧栏）

- 从 Postgres 读 `chat` 表，按 updated_at 倒序
- 每个 chat 记录：`id, agent_name, title, created_at, updated_at`
- 切换 chat → 加载 conversation 表里属于这个 chat_id 的所有 turn
- 新建 chat：在 Postgres `chat` 表 insert 一条，记当前选中的 agent

#### 11.3 Chat Window

- 主区域显示 conversation 表里这个 chat 的所有 turn，按 ts 排序
- 输入框 → 用户敲消息 → 触发：
  1. 在 Postgres 写一条 `conversation` row（role=user）
  2. 构造完整 messages 数组（DB 里这个 chat 的所有历史）
  3. `POST /api/chat` 到当前选中 agent 的 base URL，body `{messages, trace_id: uuid()}`
  4. 打开 SSE EventSource 接收事件
  5. 实时渲染 text_delta（typing 效果）、tool_call、tool_result
  6. 收到 `done` 后：
     - 把这一 round 的 assistant message + tool_results 写入 Postgres
     - 关 EventSource
     - 如果 stop_reason=`hitl_timeout`：显示提示"上次操作超时，重新发送即可继续"

#### 11.4 HITL Modal

- SSE 收到 `confirm_request` 事件 → 弹出模态框
- 显示 `tool_name` + `preview` + 完整 `tool_args`（JSON pretty print）
- 按钮：`[Allow] [Deny]`
- 倒计时显示剩余秒数（基于 `hitl_timeout` from `/api/info`）
- 用户点击 → `POST /api/confirm/{request_id}` body `{allowed: true/false}`
- 收到 410 → 关 modal + 显示"已超时"
- 收到 200 → 关 modal，等 SSE 流继续推 `tool_result`

**关键约束**：同一个 chat 同时只会有一个 confirm_request（agent_loop 串行执行 tool）。所以 modal 不需要队列。但**多个 chat 同时打开**时各自的 modal 互不干扰（后端用 `request_id` 路由）。

#### 11.5 Skill Picker（可选 UX）

- 用户敲 `/` 时弹出 `GET /api/skills` 返回的 skills 列表
- 选中一个 → `GET /api/skills/{name}` 拿全文 → 自动插入到输入框
- 用户在前面/后面加自己的话，提交

或者更简单：在 chat window 顶部放一个 dropdown "📚 Use skill"。

### 11.6 Conversation 数据 schema 建议

```sql
create table chat (
    id          uuid primary key,
    agent_name  text not null,           -- 来自 /api/info
    agent_url   text not null,           -- 这个 chat 绑定的容器 URL
    title       text,                    -- 用户起名 or LLM 自动生成
    created_at  timestamptz default now(),
    updated_at  timestamptz default now()
);

create table conversation (
    id          uuid primary key,
    chat_id     uuid references chat(id) on delete cascade,
    role        text not null,           -- 'user' | 'assistant'
    content     jsonb not null,          -- Anthropic-shaped content blocks
    -- content 例子：
    --   user msg:        "你好"
    --   assistant msg:   [{type:"text",text:"..."},
    --                     {type:"tool_use",id,name,input}]
    --   tool_result msg: [{type:"tool_result",tool_use_id,content}]
    ts          timestamptz default now()
);
create index on conversation (chat_id, ts);
```

发起 chat 请求时把这个 chat 所有 conversation rows 按 ts 升序读出来，组成 `messages` 数组（每一个 row.content 就是一条 message）。

### 11.7 错误 / 边界处理

- 容器返回 5xx / SSE 断开：显示"连接失败，重试"，按钮 → 重新打开 SSE 同 trace_id
- HITL modal 用户关浏览器 → SSE 断开 → 后端会自动 cancel → 下次进 chat 看到上一轮被 abort 的痕迹
- agent 返回 `error` 事件 → toast 显示错误信息 + 把这一 round 标记为失败（不写入 conversation）
- 长时间没收到事件（>30s 无 ping）→ SSE 死了，重连
- 切换 agent 后是否带 history：默认**不带**（不同 agent 知识不通用）；可以加个"从此 chat 复制对话开新 chat"的按钮

### 11.8 实现注意

- **EventSource API 限制**：原生 `EventSource` 不支持 POST。必须用 `fetch` + `ReadableStream` + 手动 SSE parser。或用 `@microsoft/fetch-event-source` 这个库。
- **CORS**：本地 dev 时后端 `AGENT_CORS_ORIGINS=http://localhost:3000`
- **重连 trace_id**：同一个 chat 中断后重连，复用同一个 trace_id 让 backend 能正确清理上次的 confirm slots

---

## 12. 仓库结构

```
agent-runtime/
├── agent_runtime/                     ← Python 包
│   ├── __init__.py
│   ├── core/                          ← 同步内核（不要轻易动）
│   │   ├── __init__.py
│   │   ├── config.py                  ← env 解析、Anthropic client、settings 解析
│   │   ├── loop.py                    ← agent_loop（streaming + tools + HITL）
│   │   ├── tools.py                   ← bash/read/write/edit/todo/load_skill 实现 + dispatch
│   │   ├── mcp_client.py              ← MCPManager（后台 event loop 桥接）
│   │   ├── hooks.py                   ← PreToolHook + HookManager + AbortRound + LogHook
│   │   ├── session.py                 ← (已删)
│   │   ├── compression.py             ← (已删)
│   │   ├── skills.py                  ← SkillLoader（读 SKILL.md）
│   │   ├── tracking.py                ← TokenTracker, 价格表
│   │   └── todo.py                    ← Todo 数据类
│   ├── api/                           ← FastAPI 层
│   │   ├── __init__.py
│   │   ├── app.py                     ← FastAPI() + lifespan + CORS + logging.basicConfig
│   │   ├── schemas.py                 ← SSE event dataclasses
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── meta.py                ← /healthz /info /tools /skills /skills/{name}
│   │       ├── chat.py                ← POST /chat (SSE)
│   │       └── confirm.py             ← POST /confirm/{req_id}
│   └── engine.py                      ← AgentEngine, _ConfirmRegistry, _RegistryConfirmHook
│
├── agents/                            ← 每个 agent 一个目录
│   ├── README.md
│   ├── base.Dockerfile                ← 基础镜像
│   └── adf-agent/                     ← 示例 agent
│       ├── Dockerfile
│       ├── pyproject.toml             ← 这个 agent 自己的额外 deps
│       ├── uv.lock                    ← 锁定版本
│       ├── skills/{adf-overview, find-pipelines-by-service, test-linked-service}/SKILL.md
│       ├── settings/{mcp.json, HITL.json}
│       ├── prompts/system.md
│       └── mcp/adf_mcp_server.py      ← Azure ADF MCP server
│
├── agent_frontend/                    ← (placeholder, broken, 待删/待重写)
├── tests/                             ← (broken, 暂未修)
├── doc/
│   ├── design.md                      ← 本文档
│   ├── follow-ups.md                  ← 已知遗留清单
│   ├── compression.md                 ← 历史设计文档（compaction 已移除）
│   └── cli-frontend.png               ← 老 CLI 截图
├── prompt/                            ← 早期设计 prompts（历史档案）
├── pyproject.toml
├── uv.lock
├── .env                               ← 本地 dev 用（含 ANTHROPIC_API_KEY 等）
├── .gitignore
└── .dockerignore
```

---

## 13. 已知遗留问题

详见 [`doc/follow-ups.md`](follow-ups.md)。

**已修复**（按优先级）：
- ✅ Per-chat 状态污染（TODO / Tracker / `_on_event`）
- ✅ Stateless 化（删 SessionStore）
- ✅ Auto-compact 整体移除（"以后再想 solution"）
- ✅ Per-agent uv lockfile
- ✅ /api/info 瘦身 + AGENT_NAME
- ✅ Logging 替换 print + ANSI

**仍待处理**（按优先级）：

| # | 问题 | 优先级 | 大致工作量 |
|---|---|---|---|
| 6 | `compression.auto_compact` 用 sync Anthropic client | 🟡 (moot, 已删) | — |
| 7 | `agent_frontend/` import 必坏 | 🟡 | 5 min（git rm） |
| 8 | `tests/test_integration.py` import 老路径 | 🟡 | 5 min（git rm 或重写） |
| 9 | CORS=`*` 无 auth | 🟡 | 30 min（API key middleware） |
| 12 | SSE 契约文档 | 🟡 | (已在本文档第 9 节覆盖) |
| 13 | SSE keep-alive vs reverse proxy | 🟢 | 文档化 |
| 14 | MCP shutdown 5s timeout | 🟢 | 1 行改动 |
| 15 | `tracking.py` 价格表硬编码 | 🟢 | 维护性 |
| 16 | `agent_loop` 修改 messages in-place | 🟢 | 已在文档 11.6 说明 |
| 17 | `build_system_prompt` 仅 init 时跑一次 | 🟢 | 文档化即可 |
| 19 | ThreadPool 非 daemon → 关闭挂起 | 🟡 | 30 min（cancel_all + wait=True） |
| 20 | `asyncio.get_event_loop()` deprecated | 🟢 | 1 行改动 |
| 21 | SSE queue 满静默丢事件 | 🟢 | 加 logger.warning |

**前端开始之前必做的**：#7（删 broken frontend），#9（最少加个 API key），#19（生产部署前必须）。
**前端可以并行不影响的**：所有 🟢 项。

---

## 14. 如何扩展

### 加一个新 agent

1. 复制 `agents/adf-agent/` → `agents/<name>-agent/`
2. 改 `pyproject.toml` 列出这个 agent 需要的额外 deps（如果它有 MCP server 的话）
3. `cd agents/<name>-agent && uv lock`
4. 改 `Dockerfile`：`ENV AGENT_NAME=<name>`、`COPY` 路径调整
5. 把 `skills/` 换成这个 agent 的 SKILL.md 们
6. 改 `settings/mcp.json` 配它的 MCP server
7. 改 `settings/HITL.json` 决定哪些 tool 要确认
8. 改 `prompts/system.md` 写 agent identity
9. Build & run on a different host port

### 加一个新 builtin tool

1. `agent_runtime/core/tools.py` 加 schema 到 `BUILTIN_TOOLS`
2. 加 handler 到 `TOOL_HANDLERS`
3. 如果工具改了文件系统/数据库，考虑加进 `HITL.json`

### 加一个新 SSE event 类型

1. `agent_runtime/api/schemas.py` 加 dataclass
2. `agent_runtime/engine.py` 的 `_EVENT_MAP` 加 factory
3. `agent_runtime/core/loop.py` 在合适位置 `on_event(...)` 触发
4. 更新本文档第 9 节的契约表
5. 通知前端

### 启用 thinking

直接在 `.env` 或 container env 里设 `AGENT_THINKING=1` + `AGENT_THINKING_BUDGET=10000`。重启容器生效。前端会通过 SSE 收到 `thinking_*` 事件。

### Native async 升级

未来如果并发到了上千需要：
1. `core/loop.py` `_stream_response` 用 `AsyncAnthropic`
2. `core/tools.py` `subprocess.run` → `asyncio.create_subprocess_shell`，文件 IO → `anyio.Path`
3. `core/mcp_client.py` 删 background thread，全用 `AsyncExitStack` + `lifespan`
4. `engine.py` 删 ThreadPoolExecutor，`chat_stream` 直接 `async for event in agent_loop(...)`
5. HITL hook 用 `asyncio.Future` 替代 `threading.Event`

API contract（第 8、9 节）**完全不变**，前端无感知。
