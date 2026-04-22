# Auth 设计最终方案（v1）

## 目标

让多用户安全地并发使用 agent container，下沉用户身份用于 cache 分桶、admission、audit；下游数据访问默认走 container 自己的 service credential，允许每个 agent 在 JSON 里声明"某些 provider 必须走用户身份"（device code login）。

**核心原则**：auth 和 MCP 完全解耦，auth 是独立 top-level 包，runtime 冻结业务逻辑。

---

## 模块布局

```
agent-runtime/
├── agent_frontend/            # 解 EasyAuth header、转发 Bearer 给 runtime
├── agent_runtime/             # 验 JWT、注入 token 给 MCP、托管 device flow plumbing
├── auth/                      # ★ 新建，top-level 独立包
│   ├── identity.py            # UserIdentity dataclass + JWT 验签
│   ├── easyauth.py            # 解 X-MS-* header → UserIdentity（复用 opsagent2 那 20 行）
│   ├── cache.py               # dict[(user_id, provider), TokenRecord] + threading.Lock
│   ├── config.py              # 加载 auth.json
│   └── providers/
│       ├── base.py            # Provider 抽象：get_credential(user_id) -> Credential
│       ├── azure_service.py   # DefaultAzureCredential 包一层
│       ├── azure_device.py    # Azure device code flow
│       ├── snowflake_device.py
│       └── ado_device.py
└── agents/
    └── adf-agent/
        └── settings/
            ├── mcp.json
            ├── HITL.json
            └── auth.json      # ★ 新增：声明这个 agent 要用哪些 provider
```

---

## auth.json schema（每个 agent 一份）

```json
{
  "providers": {
    "azure": {
      "mode": "service"
    },
    "azure-privileged": {
      "mode": "device_code",
      "scope": "https://management.azure.com/.default",
      "tenant": "privileged-tenant-id"
    },
    "snowflake": {
      "mode": "device_code",
      "account": "xy12345.us-east-1",
      "role": "ANALYST"
    }
  },
  "mcp_bindings": {
    "adf":       "azure",
    "snowflake": "snowflake"
  }
}
```

**约定**：
- `mode` 只有 `"service"` 或 `"device_code"`（OBO 推迟到 v2）
- provider entry 缺省 = `mode: service`
- `mcp_bindings` 把 MCP server 名字映射到 provider 名字（runtime 在注入 token 时查这张表）

---

## 两种身份，职责分离

| 概念 | 来自哪 | 用来干嘛 |
|---|---|---|
| **User identity** | EasyAuth → JWT 验签 | Cache 分桶、SSE routing、admission、audit |
| **Service credential** | 容器自己的 Managed Identity / service account | `mode: service` 的 provider 调下游 |
| **User's device token** | 用户显式做 device code login | `mode: device_code` 的 provider 调下游 |

**关键点**：EasyAuth 的 access token **不再往下游穿**。ID token 只在 runtime 入口验身份。下游 credential 要么是 service 的，要么是 device flow 拿到的。

---

## 数据流

```
User ──▶ EasyAuth ──▶ agent_frontend
                         │
                         │ 从 X-MS-TOKEN-AAD-ID-TOKEN 拿 JWT
                         ▼
                    Authorization: Bearer <id_token>
                         │
                         ▼
                    agent_runtime
                         │
                         │ ① require_user dependency:
                         │      验 JWT 签名、iss、aud、exp
                         │      → UserIdentity → ContextVar
                         │      (user_id 从此可信)
                         │
                         │ ② agent 跑、call MCP tool
                         │      runtime 查 auth.json 的 mcp_bindings
                         │      找到这个 MCP 绑的 provider name
                         │
                         ├── provider.mode == "service"
                         │       auth.get_credential(None, provider)
                         │       → DefaultAzureCredential / service account
                         │       → 注入进 MCP tool args ──▶ 下游
                         │
                         └── provider.mode == "device_code"
                                 auth.get_credential(user_id, provider)
                                 ├─ cache hit  → 注入 ──▶ 下游
                                 └─ cache miss → 触发 device flow
                                                    │
                                                    │ (用 HITL 同构的 pending-action
                                                    │  通过 SSE 把 device URL + user_code
                                                    │  推给 frontend)
                                                    │
                                                    ▼
                                               用户新 tab 完成 login
                                                    │
                                                    ▼
                                               runtime poll 到 token → cache
                                                    → 继续注入 ──▶ 下游
```

---

## 改动清单

### 新增文件

| 文件 | 职责 |
|---|---|
| `auth/identity.py` | `UserIdentity` dataclass，`validate_jwt(token) -> UserIdentity` |
| `auth/easyauth.py` | `parse_easyauth_headers(headers) -> UserIdentity`（含本地 dev bypass） |
| `auth/cache.py` | In-process token cache，`get/put/invalidate`，加锁 |
| `auth/config.py` | `load_auth_config(path) -> AuthConfig` |
| `auth/providers/base.py` | Provider 抽象 + `ContextualCredential` helper（thread-local token） |
| `auth/providers/azure_service.py` | 包装 `DefaultAzureCredential` |
| `auth/providers/azure_device.py` | Azure device code state machine |
| `auth/providers/snowflake_device.py` | 同上 |
| `auth/providers/ado_device.py` | 同上 |
| `agents/adf-agent/settings/auth.json` | 当前 agent 的 provider 声明 |

### 修改文件

| 文件 | 改什么 |
|---|---|
| `agent_frontend/server.py:46` | 删 `_DEFAULT_USER_ID`，所有 route 加 `user: UserIdentity = Depends(require_user)` |
| `agent_frontend/server.py:225` | chat proxy 转发时把 EasyAuth 的 id_token 作为 `Authorization: Bearer` 传 runtime |
| `agent_runtime/api/routes/chat.py` | 加 `require_user` dependency，验 JWT → set ContextVar |
| `agent_runtime/api/routes/confirm.py` | 同上（确认 HITL 归属） |
| `agent_runtime/api/routes/meta.py` | 同上（`/api/info` 可免 auth） |
| `agent_runtime/engine.py:316-408` | `chat_stream` 接收 `UserIdentity`；`_run_sync` 在 executor 线程里 **重新 set ContextVar**（跨线程不会自动带） |
| `agent_runtime/engine.py:_ConfirmRegistry` | 泛化或复制一份给 device flow 的 pending-action 用 |
| `agent_runtime/core/tools.py` | MCP tool call 前 middleware：读 ContextVar → 查 mcp_bindings → `auth.get_token()` → 注入进 tool args |
| `agent_runtime/core/config.py` | 新增 `resolve_auth_config()`，读 `settings/auth.json` |
| `agents/adf-agent/mcp/adf_mcp_server.py:26-35` | 删 `_client = DefaultAzureCredential()` singleton，改 `ContextualCredential` 模式：共享一个 `DataFactoryManagementClient`，credential 的 `get_token` 从 thread-local 读 |
| `agents/adf-agent/mcp/adf_mcp_server.py` tools | 每个 `@mcp.tool` 接收隐式 `_auth_token` 参数（用装饰器批量注入） |
| `agents/base.Dockerfile` | COPY `auth/` 包进 container |

### 不变

- skills/、prompts/system.md
- mcp.json、HITL.json 格式
- 现有 HITL primitive（device flow 复用它的架构，同构）

---

## 安全关键点

1. **JWT 验签必做**：runtime 入口验签是唯一让 `user_id` 可信的方式，直接读 header 会被伪造 → cache 污染 → 跨用户 token 泄漏。
2. **Cache key 必须是 `(user_id, provider)`**：不按 user 分桶 = data leak。
3. **MCP subprocess 不持有任何 user 状态**：`ContextualCredential` 每次 call 从 thread-local 读当前请求的 token，subprocess 本身无状态。
4. **service credential 不挂 user_id**：key 用 `None` 或固定常量，所有人共享（符合"container 作为 actor"语义）。
5. **ContextVar 跨线程不自动传**：`engine.py` 的 `run_in_executor` 边界必须手动 propagate。

---

## 不在 v1 范围

- **OBO flow**：device code 能覆盖所有场景，UX 成本换来的是架构极大简化。需要 OBO 时只是多加一个 `mode: "obo"` provider，不改架构。
- **JWT audience 隔离（Option B）**：runtime 和 frontend 共用一个 AAD app registration。`AUDIENCE` 做成 env var，未来拆分时只改环境变量。
- **外部 token store（Redis / Key Vault）**：in-process dict 够用。多 worker 路由不一致时用 sticky session。
- **多 instance 横向扩展的 cache 一致性**：同上，sticky session。
- **Token auto-refresh**：device code 拿到 refresh_token，但 v1 可以 lazy —— 过期就让 user 重新 device login。
- **Per-user admission control**：v1 只分"已认证/未认证"，不做"哪些 user 能用哪个 agent"。

---

## 开发顺序建议（一个 PR 一步，可分批 review）

1. **PR1：`auth/identity.py` + `auth/easyauth.py` + frontend 接入**
   - 落 `UserIdentity`、JWT 验签、dependency
   - frontend 删 `_DEFAULT_USER_ID`
   - 先做到 "user_id 从 EasyAuth 下沉到 runtime 的 ContextVar"，下游还用老逻辑
2. **PR2：`auth/cache.py` + `auth/providers/base.py` + `azure_service.py`**
   - 抽象 provider，把现有 `DefaultAzureCredential` 包成 `azure_service` provider
   - 行为不变，但代码结构就位
3. **PR3：MCP `ContextualCredential` 改造**
   - `adf_mcp_server.py` 删 `_client` singleton，改 thread-local
   - runtime 端加 tool call middleware 注入 token
   - 这一步是 "MCP decouple from auth" 的实际落地
4. **PR4：`auth.json` + `azure_device.py` + device flow SSE event**
   - 第一个 device code provider 跑通（挑 Azure 先）
   - HITL primitive 泛化 / 复用
5. **PR5：Snowflake / ADO provider**
   - 到这一步加新服务 = 新 provider module + JSON 一行，runtime 零改动，验证架构

---

## 需要你最终拍板的 5 点

1. `auth/` 独立 top-level 包 ✅（已定）还是塞进 `agent_runtime/core/auth.py`？
2. MVP 跳过 OBO，只做 service + device_code ✅（已定）？
3. MVP 跳过 JWT 验签（仅网络层隔离），直接信 header？—— 我建议**做验签**，多 50 行代码买长期正确性
4. Runtime 和 frontend 共用一个 AAD app registration（Option A）✅（已定），env var 预留升级路径
5. PR 拆分节奏同意吗？还是想合并几个？

你确认完我就开 PR1。
