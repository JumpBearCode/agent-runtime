# Auth Flow —— 多用户身份、Token 管理与 MCP 解耦

> 本文档记录了 agent-runtime 接入企业 SSO（Azure EasyAuth）和多用户 auth 的完整设计过程：
> - 出发点是什么、我们一开始打算怎么做（OBO 方案）
> - 为什么最后放弃了 OBO，选择更简单的 "Managed Identity + Device Code" 方案
> - 为什么没有 OBO 还必须验 JWT
> - MCP 和 auth 怎么解耦，避免 per-user token 污染
> - 最终的 `auth.json` schema 和模块划分
>
> **状态**：设计定稿，实现未开始。
> **相关文档**：
> - [`doc/design.md`](design.md) —— runtime 整体架构，理解 engine / tools / MCP 三层关系的前提
> - [`doc/follow-ups.md`](follow-ups.md) —— 已知遗留项，本次设计补的是其中"多用户 / auth"那一块
>
> **读这份文档的人会得到什么**：
> - 理解 OAuth2 / AAD 里 audience、app registration、OBO 这些术语不是在瞎起名
> - 看到为什么 "把 header 原样传下去" 是 data leak 的雷
> - 知道 MCP subprocess 这个架构 constraint 怎么和 per-request auth 不冲突
> - 有一份可以直接开工的 change list

---

## 1. 需求与背景

### 1.1 现状

- `agent_frontend/server.py:46` 硬编码 `_DEFAULT_USER_ID = "local"`，系统假定单用户。
- `agents/adf-agent/mcp/adf_mcp_server.py:26-35` 在 MCP subprocess 启动时 `_client = DataFactoryManagementClient(DefaultAzureCredential(), ...)`，**身份被钉在 container 的 managed identity 上**。
- `agent_runtime` 的 `/api/chat` 不读任何 auth header；engine 里没有"当前用户"的概念。

对单租户本地开发这 OK，对生产部署（Azure App Service，多用户 SSO 进来）完全不行。

### 1.2 明确的需求

1. **SSO identity 下沉**：用户通过 Azure App Service EasyAuth 登录后，identity 要能在 container 里被 agent 层看到、使用。
2. **Container-level 额外 auth**：部分 agent 需要访问 EasyAuth 覆盖不到的服务：
   - (a) **非 Azure 服务**：Snowflake、Azure DevOps。它们有自己的 SSO，Azure 的 token 用不上。
   - (b) **Azure 换号**：用户用普通账号登 App Service，但想用**特权账号**访问某些数据。即使同是 Azure，也需要重新 authenticate。
3. **Plugin 式声明**：每个 agent 应该能通过配置文件（类似现有的 `mcp.json` / `HITL.json`）声明"我这个 agent 要用哪些 auth provider"，不改 runtime 代码。
4. **MCP 与 auth 解耦**：auth 逻辑不能和 MCP tool 粘在一起。MCP subprocess 是 long-lived 共享的，per-user token 必须从外面注入。

### 1.3 非目标

- **不**做自建 identity provider。所有身份来自 Azure AD 或各家服务自己的 OAuth endpoint。
- **不**做跨 container 的 token 共享。单 container / 单 worker 内 in-memory 就行，scale-out 是未来问题。
- **不**做 token 持久化（container 重启 = 用户重新登录）。对 chat session 场景可接受。

---

## 2. 为什么"把 header 原样往下传"不行

新手直觉：EasyAuth 给了 `X-MS-CLIENT-PRINCIPAL-ID` header，把它读出来，MCP 调用时带着，不就完事了？

这个方案踩三个雷。

### 2.1 雷 1：MCP 固化了身份

现在的 `adf_mcp_server.py:26-35`：

```python
_client: DataFactoryManagementClient | None = None

def _get_client():
    global _client
    if _client is None:
        credential = DefaultAzureCredential()
        _client = DataFactoryManagementClient(credential, SUBSCRIPTION_ID)
    return _client
```

这段代码的问题：**MCP 是 container 启动时就 spawn 的 long-lived subprocess**。`_client` 是模块级全局，**第一次 tool call 时创建，之后所有用户的请求都复用它**。

如果我们改成用"当前请求用户"的 credential 初始化这个 client：

```
T1: 用户 A 的请求 → _client 还是 None → 用 A 的 credential 建 → cache
T2: 用户 B 的请求 → _client 非 None → 复用 A 的 credential → ❌ B 在用 A 的身份访问 ADF
```

这是 **data leak**。A 在 Azure 审计日志里背锅，B 实际做了操作。

### 2.2 雷 2：`_DEFAULT_USER_ID` 的模块级耦合

`agent_frontend/server.py:46` 这一行：

```python
_DEFAULT_USER_ID = os.getenv("CHAT_USER_ID", "local")
```

所有 session CRUD 用这个 user_id。多用户上线后如果不改，10 个人的 session 全部挤在同一个 `"local"` 桶里，谁都能看谁的历史。

### 2.3 雷 3：header 本身不可信

`X-MS-CLIENT-PRINCIPAL-ID` 是个字符串 header。**只有请求真的经过 EasyAuth middleware 才会被 Azure 注入**。但如果 container 暴露了非 EasyAuth 的路径（sidecar、VNet 直连、内网 SLB），攻击者可以随便塞：

```
curl -H "X-MS-CLIENT-PRINCIPAL-ID: <别人的 oid>" https://runtime/api/chat ...
```

→ runtime 信以为真 → 用别人身份访问数据。这也是 data leak。

**结论**：multi-user 不是"加一行 header 解析"的事，是需要系统性设计的。

---

## 3. Azure App Service 的并发模型（先打消误解）

设计前先把一个常见误解澄清掉。有人会担心："一个 App Service instance 是不是同时只能 serve 一个用户？不同用户的 identity 会不会互相覆盖？"

**不会**。Azure App Service 和任何标准 web server 一样：

- 一个 instance = 一个跑着你代码的进程（uvicorn）。**同时并发 serve 所有用户**。
- EasyAuth 是 **per-request** 注入 header，不是 per-instance。同一瞬间 instance 里可能有 N 个 in-flight 请求，每个带各自用户的 header。
- 没有 "instance 锁定在某个用户身上" 的概念。
- 流量大了 App Service 会横向扩（scale-out），每个 instance 照样并发 serve 多人。

**真正的"race condition"在你的代码里**，不在 Azure 层。具体要盯的：

1. 任何**模块级 / 进程级**的变量：`_DEFAULT_USER_ID`、`_client`、各种 singleton cache。
2. 如果挂着用户数据就必须按 `user_id` 分桶（用 dict 而不是单值），或者用 `ContextVar` 只在当前请求可见。
3. MCP subprocess 共享**没问题**，关键是它内部不能 cache 任何 per-user 的东西。

这条一旦想清楚，后面的设计就是"怎么让所有的 per-user 状态都正确分桶"。

---

## 4. OAuth2 / AAD 核心概念（工具箱）

后面讨论 OBO、audience、device code 都用得上这几个概念。一次讲清。

### 4.1 AAD App Registration

AAD App Registration **不是**一台服务器，是 Azure AD 里的一条**元数据记录**：

```
┌─ Azure AD 里存着一条记录 ─────────────────────┐
│  client_id    = abc-123-...                 │
│  redirect_uri = https://myapp.com/...       │
│  client_secret= xyz-secret (可选)           │
│  scopes       = [openid, profile, ...]      │
│  exposed_api  = [...] (如果这个 app 被别人调) │
└─────────────────────────────────────────────┘
```

- `client_id` = 这个 app 的身份证号
- `client_secret` = 这个 app 的密码，向 AAD 证明自己确实是它
- **app registration 是 AAD 认识你的 app 的凭据**

EasyAuth 配置的时候填的就是一个 app registration 的 client_id 和 secret。

### 4.2 Token 的 audience（`aud` claim）

每张 access token 都有个 `aud` 字段，相当于"这张票只能进这个剧场"。

```
user_token = {
  "sub": "user-oid-abc",                        // 谁
  "aud": "https://management.azure.com/",       // 给谁用 ← audience
  "iss": "https://sts.windows.net/tenant/",     // AAD 发的
  "exp": 1234567890,
  ...
}
```

**一张 token 只能给一个 audience 用**。调 ADF 要 `aud=https://management.azure.com/`，调 Storage 要 `aud=https://storage.azure.com/`，调自己的 app 要 `aud=<自己 app 的 client_id>`。**audience 不对就拒绝**。

换 audience 的方式：找 AAD 换。参见下面 OBO。

### 4.3 EasyAuth 做的事

EasyAuth 是 App Service 层的 middleware。它替你做了：

1. 拦截未登录的请求，302 重定向到 AAD 登录页
2. 用户登录后，AAD 把 authorization code 回调到 EasyAuth 的 `/callback` endpoint
3. EasyAuth 拿 code 去 AAD 换 token（用你 app registration 的 secret）
4. 把 token 存进 EasyAuth 的 "token store"，给浏览器发 session cookie
5. 后续请求带着 cookie → EasyAuth 查到 token → 注入 header → 转发给你的 app

你的 app 看到的最终请求里会有：

```
X-MS-CLIENT-PRINCIPAL-NAME       : 用户 UPN / email
X-MS-CLIENT-PRINCIPAL-ID         : 用户 oid
X-MS-CLIENT-PRINCIPAL            : base64(JSON) 的完整 claims
X-MS-TOKEN-AAD-ID-TOKEN          : OIDC id_token（JWT，带签名）
X-MS-TOKEN-AAD-ACCESS-TOKEN      : OAuth2 access_token（aud=你的 app）
X-MS-TOKEN-AAD-REFRESH-TOKEN     : refresh token
```

**关键**：前三个 header 是 EasyAuth "好心解好的便利信息"，**没签名可伪造**。后三个是 AAD 签过名的原始 token，**有签名可验证**。

---

## 5. 完整登录流程可视化

这一节是理解后面所有讨论的基础。把 "用户点进 App Service → EasyAuth 和 AAD 来回几次 → 最后 app 看到带 header 的请求" 这个过程完整画出来。

### 5.1 Phase 0 —— 部署前（一次性）

```
   [你/运维]                      [Azure AD]
     │                                │
     │  "我要注册一个 app"             │
     │───────────────────────────────▶│
     │                                │─┐ 创建记录：
     │                                │ │   client_id = abc-123...
     │                                │ │   redirect_uri = https://myapp.com/.auth/login/aad/callback
     │                                │ │   scopes = [openid, profile, email]
     │                                │ │   client_secret = xyz
     │                                │◀┘
     │  "好了，client_id = abc-123"   │
     │◀───────────────────────────────│
     │                                │
     │  把 client_id + secret 配到    │
     │  App Service 的 EasyAuth 里    │
     ▼                                │
```

### 5.2 Phase 1 —— 用户首次访问（Authorization Code Flow）

```
User(browser)         EasyAuth          Azure AD           Your App
     │                   │                  │                  │
 ①   │  GET https://myapp.com/              │                  │
     │──────────────────▶│                  │                  │
     │                   │                  │                  │
 ②   │  302 → login.microsoftonline.com                        │
     │  ?client_id=abc-123&redirect_uri=...&scope=openid...    │
     │◀──────────────────│                  │                  │
     │                                      │                  │
 ③   │  GET login.microsoftonline.com/...   │                  │
     │─────────────────────────────────────▶│                  │
     │                                      │                  │
 ④   │  [AAD 显示登录页，用户输密码 / MFA]   │                  │
     │◀─────────────────────────────────────│                  │
     │                                      │                  │
 ⑤   │  POST 用户名 + 密码                   │                  │
     │─────────────────────────────────────▶│                  │
     │                                      │─┐ 验证通过       │
     │                                      │ │ 生成 auth_code │
     │                                      │◀┘                │
     │                                      │                  │
 ⑥   │  302 → redirect_uri?code=AUTH_CODE   │                  │
     │◀─────────────────────────────────────│                  │
     │                   │                  │                  │
 ⑦   │  GET myapp.com/.auth/login/aad/callback?code=AUTH_CODE  │
     │──────────────────▶│                  │                  │
     │                   │                  │                  │
 ⑧   │                   │  POST /token     │                  │
     │                   │  code=AUTH_CODE  │                  │
     │                   │  + client_secret │                  │
     │                   │─────────────────▶│                  │
     │                   │                  │─┐ 验证           │
     │                   │                  │◀┘                │
 ⑨   │                   │  返回 id_token / access_token / refresh_token
     │                   │◀─────────────────│                  │
     │                   │─┐ 存进 EasyAuth  │                  │
     │                   │ │ token store    │                  │
     │                   │◀┘                │                  │
     │                   │                  │                  │
 ⑩   │  302 原始 URL + Set-Cookie           │                  │
     │◀──────────────────│                  │                  │
     │                   │                  │                  │
 ⑪   │  GET myapp.com/ (带 cookie)          │                  │
     │──────────────────▶│                  │                  │
     │                   │─┐ 从 cookie 找到 │                  │
     │                   │ │ token，注入 header                │
     │                   │◀┘                │                  │
     │                   │  forward to app  │                  │
     │                   │─────────────────────────────────────▶│
     │                   │                  │  [app 看到带 X-MS-* header 的请求]
```

**关键观察**：

- **EasyAuth 做的就是第 ② ~ ⑩ 步的整套舞蹈**。没它你得自己写一遍 OAuth2 authorization code flow。
- **App 从来没碰过密码**。验密码是 AAD 的事，app 只在第 ⑪ 步看到已认证的 header。
- **第 ⑨ 步返回的 `access_token`，`aud` 是你的 app（abc-123）**。这就是下一节讨论的核心：**这张 token 不能直接用来调 ADF**，因为 `aud` 不对。

### 5.3 Phase 2 —— 直接用当前 token 调下游（仅当 audience 正好匹配时）

假如下游服务正好认你 app 的 audience（罕见）：

```
Your App                        Azure Service (aud=myapp)
   │                                   │
   │  Authorization: Bearer <access_token>
   │──────────────────────────────────▶│
   │                                   │─┐ 验证签名、aud、exp、RBAC
   │                                   │◀┘
   │  200 OK + 数据                     │
   │◀──────────────────────────────────│
```

真实场景里基本用不到，因为 ADF、Storage、Key Vault 各有自己的 audience。所以引出 Phase 3：**OBO**。

---

## 6. OBO —— On-Behalf-Of Flow（我们一度打算用的方案）

### 6.1 OBO 是什么

**核心概念**：一张 token 只能给一个 audience 用，要换 audience 就去 AAD 换一张新的。

OBO 是这个"换"的官方命名，用在**中间层 app 代表用户调下游**的场景：

```
User ──登录──▶ 中间层 app (有 user_token, aud=中间层)
                  │
                  │ "我想代表这个用户调 ADF"
                  │
                  ▼
              Azure AD: "你带着 user_token 来，+ 你的 client_secret
                         证明你是正版，我给你一张新 token，
                         aud=ADF，身份还是那个用户"
                  │
                  ▼
              new_token (aud=ADF, sub=原用户的 oid)
                  │
                  ▼
              调 ADF ──▶ ADF 以该用户身份授权
```

### 6.2 OBO 流程详细可视化

```
Your App                Azure AD                     Azure Service (ADF)
   │                       │                              │
   │  当前持有：            │                              │
   │  user_token (aud=myapp)                              │
   │                       │                              │
   │  POST /oauth2/token   │                              │
   │  grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer
   │  (也叫 on_behalf_of grant)                            │
   │  assertion=user_token (aud=myapp)                    │
   │  requested_scope=https://management.azure.com/.default
   │  client_id=myapp                                     │
   │  client_secret=xyz                                   │
   │──────────────────────▶│                              │
   │                       │─┐ AAD 验证三件事：            │
   │                       │ │  1. user_token 签名对吗    │
   │                       │ │  2. myapp 有 API permission│
   │                       │ │     调 ADF 吗（管理员配置） │
   │                       │ │  3. 这个用户在 ADF 上有权限 │
   │                       │◀┘                            │
   │                       │                              │
   │  新 token：            │                              │
   │  obo_token (aud=https://management.azure.com/)       │
   │  sub = 原来那个用户的 oid（身份不变）                   │
   │◀──────────────────────│                              │
   │                       │                              │
   │  GET /adf/pipelines                                  │
   │  Authorization: Bearer <obo_token>                   │
   │─────────────────────────────────────────────────────▶│
   │                       │                              │─┐ 验签 + RBAC
   │                       │                              │◀┘ (以该用户身份)
   │  200 OK                                              │
   │◀─────────────────────────────────────────────────────│
```

**OBO 本质 = "换 audience 的 token 交换"**，身份（`sub`/`oid`）贯穿不变。

### 6.3 我们原本打算怎么用 OBO

最初的设计是：

```
EasyAuth ──▶ user_token (aud=myapp)
               │
               ▼
           frontend 转发到 runtime
               │
               ▼
           runtime 做 OBO: user_token → obo_token (aud=ADF)
               │
               ▼
           注入到 MCP tool call 的参数里
               │
               ▼
           MCP 用这张 token 调 ADF
```

好处：**用户无感**。已经登录 Azure 了，自动拿到下游 token，不用再登一次。

这就是我们讨论了很久的方案。但最终放弃了。

### 6.4 Audience 策略讨论：Option A vs Option B

OBO 方案下还有一个子问题：**runtime 验的 audience 是什么？**

**Option A：frontend 和 runtime 共用一个 AAD app**

```
       ┌─ 一个 AAD app: "my-app" (client_id=abc) ──┐
       │                                           │
User──▶EasyAuth──▶user_token (aud=abc)             │
                     │                             │
                     ▼                             │
                  frontend─▶(原样转发)─▶runtime    │
                                          │        │
                                          ▼        │
                                    验 aud==abc ✓  │
       └───────────────────────────────────────────┘
```

Pro：简单，一份 app 注册。
Con：token 可以跨层重放，frontend 的 token 被偷也能打 runtime。

**Option B：runtime 注册独立 AAD app，frontend 做一次 OBO 再转发**

```
 ┌─AAD app: frontend-app──┐    ┌─AAD app: runtime-app──┐
 │  client_id=abc         │    │  client_id=xyz        │
 │                        │    │  exposes scope:       │
 │                        │    │    api://runtime/.default
 └───┬────────────────────┘    └───▲──────────────────┘
     │ (API permission 配好)       │
User──▶EasyAuth──▶user_token (aud=abc)
                     │
                     ▼
                  frontend 做 OBO：
                    user_token (aud=abc) → runtime_token (aud=xyz)
                     │
                     ▼
                  runtime──▶验 aud==xyz ✓
```

Pro：audience 隔离，token 不能跨层重放；service caller 可以直接要 aud=runtime 的 token，语义干净。
Con：多一个 AAD app 注册 + 多一次 OBO。

**决策**：MVP 用 A，代码结构留好将来升 B 的口子（`AUDIENCE` 做成 env var）。升 B 的触发条件：

- runtime 要被 frontend 之外的 caller 调
- security review 要求
- 多个 frontend 共用一个 runtime

这段讨论最终也随 OBO 一起被搁置（见下节）。

---

## 7. 为什么最终放弃 OBO

讨论到一半，意识到 OBO 可能是过度设计。触发点是一个简单观察：

> Device Code Login 能覆盖 OBO 的所有场景。

### 7.1 Device Code Login 是什么

一种**显式的、用户参与的**登录流程。不像 OAuth2 authorization code flow 需要浏览器重定向，device code flow 只需要：

```
1. App 问 AAD（或 Snowflake / ADO）："我要登录，给我一个 device code"
2. AAD 回复：
     device_code = "ABCD-1234"（app 保留）
     user_code   = "XYZ789"（展示给用户）
     verification_uri = "https://microsoft.com/devicelogin"
3. App 告诉用户：去那个 URL，输这个 code
4. 用户在**另一个 tab**打开，用任何账号登录，输 user_code
5. 同时 app 不断轮询 AAD："好了吗？"
6. 用户完成后，AAD 回复 app：token 在这
7. App 拿到 token，scope 是它一开始请求的那个
```

关键点：**拿到的 token 直接就是目标 scope**，不需要 OBO 换。

### 7.2 Device Code 覆盖 OBO 的两种场景

**场景 A：用户以自己 Azure 身份调 ADF**
- OBO 方案：静默换 token，无感
- Device Code 方案：用户新开 tab 走一次 device login，token scope=ADF，直接用
- 差异：UX，首次多一次登录

**场景 B：用户换特权账号调 ADF**
- OBO 方案：做不到（OBO 只能用当前登录的身份）
- Device Code 方案：用户新开 tab 用特权账号登，token 就是特权身份的
- 差异：OBO 根本不支持这场景

**场景 C：调 Snowflake / ADO 这些非 Azure 服务**
- OBO 方案：做不到（OBO 是 AAD-only）
- Device Code 方案：各家 provider 都有自己的 device flow endpoint，统一走
- 差异：OBO 不适用

所以 Device Code 是**真子集包含** OBO 的能力（除了 UX 平滑度）。

### 7.3 Managed Identity 覆盖的另一部分

同一时间意识到：企业内 agent 的大多数场景**根本不需要 per-user authZ**。典型部署：

- 一个团队部署一个 ADF agent container
- container 的 Managed Identity 有该团队 ADF 的 Reader 权限
- 团队里所有人用这个 agent 看到的视图是一样的
- "某用户查了某 pipeline" 的审计在应用层做（记 user_id + 查询内容），不需要靠 token 的 `sub` 来区分

这种场景下 **container 的 Managed Identity 就够了**，OBO 是过度设计。

### 7.4 结论：两种 mode 足够

把 OBO 从"默认方案"降级到"暂不实现"，保留的两种 mode：

| Mode | 谁是 actor | 谁发 token | 何时用 |
|---|---|---|---|
| **`service`** | Container 自己 | Managed Identity（Azure）/ Service Account（Snowflake/ADO） | 团队共享访问、per-user authZ 不重要 |
| **`device_code`** | 人类用户 | 用户在新 tab 登录 | Snowflake/ADO、Azure 特权账号、合规要求 user-level 审计 |

这个二分法的好处：

1. **零迁移**：service mode 就是现在 `adf_mcp_server.py` 的 `DefaultAzureCredential` 行为，不改现有 MCP 也能跑
2. **心智简单**："这个 provider 用 container 身份 or 用户身份"，二选一
3. **OBO 可以以后加**：如果 device code 的 UX 成本真的成为负担（用户每天登 10 次），再加 OBO 作为"对已登录 Azure 账号的 UX 优化"，加回来是**纯增量**，不动架构
4. **省了一大堆配置**：没有 API permissions、没有 OBO 专用 client_secret、没有 Option A vs B 纠结

### 7.5 什么时候该回头加 OBO

就一条触发条件：**device code login 的 UX 对用户真的成为负担**。比如：

- 用户每天用 N 次 agent，每次都要 device code 登一遍 Azure 太烦
- 多数请求需要以 user 身份调 Azure，业务允许"sso-derived 隐式身份"
- 合规要求每个 Azure 操作都必须有用户自己的 token，不接受 service account 代理

三条都不成立 → device code 够用。加 OBO 纯 UX 优化。

---

## 8. MCP 与 Auth 解耦

### 8.1 问题

回到 §2.1 的雷：`_client = DataFactoryManagementClient(DefaultAzureCredential(), ...)` 固化了身份。要让同一个 MCP 能用**当前请求用户的 token**，但 MCP 是 container 启动时就起的共享 subprocess，怎么传？

### 8.2 朴素方案：每次现建 client

最直接的做法：每次 tool call 传 token，现建 client：

```python
@mcp.tool
def list_pipelines(_auth_token: str):
    cred = StaticTokenCredential(_auth_token)
    client = DataFactoryManagementClient(cred, SUBSCRIPTION_ID)
    return list(client.pipelines.list_by_factory(...))
```

能跑，但每次重建 client 浪费（连接池、pipeline 配置都重建）。Azure SDK client 构造虽然是微秒级，连接池不能复用还是亏。

### 8.3 更好方案：ContextualCredential + thread-local token

让 client 只建一次，但它持有的 credential 是**动态的**：

```python
# MCP subprocess 启动时建一次，所有用户共用
class ContextualCredential:
    """每次 get_token 时从 thread-local 读当前请求的 token。"""
    def get_token(self, *scopes, **kwargs):
        token, expiry = _current_token.token, _current_token.expiry
        return AccessToken(token, expiry)

_credential = ContextualCredential()
_client = DataFactoryManagementClient(_credential, SUBSCRIPTION_ID)
_current_token = threading.local()

@mcp.tool
def list_pipelines(_auth_token: str, _expires_on: int):
    _current_token.token = _auth_token
    _current_token.expiry = _expires_on
    try:
        return list(_client.pipelines.list_by_factory(...))
    finally:
        _current_token.token = None  # 防泄漏
```

原理：Azure SDK client 每次 call API 时会调 `credential.get_token()` 取最新 token。让这个 `get_token()` 去读 thread-local，就能做到 "**一个共享 client 服务所有用户，每次 call 用当前请求的 token**"。

### 8.4 为什么共享 subprocess 是安全的

有人会问：多用户并发时，thread-local 会互相踩吗？不会。

Python 的 `threading.local()` 是**每个线程独立的变量空间**。MCP over stdio 的 FastMCP 实际上是在同一个进程的线程池里跑，每个请求一个线程 → `_current_token.token` 在当前线程设的值，其他线程看不见。并发安全。

**MCP subprocess "被共享" 本身不是问题，关键是"被共享的进程里不 cache 任何一个用户的身份"**。只要 MCP 不 cache credential / token，共享就是干净的。

### 8.5 Service mode 怎么办

Service mode 下 MCP 不需要接 `_auth_token`，直接用它自己的 `DefaultAzureCredential`。两种 mode 的实际差异：

```python
# service mode 的 provider（runtime 侧）返回：
DefaultAzureCredential()   # 每次都能 get_token()，不需要外部输入

# device_code mode 的 provider 返回：
StaticTokenCredential(cached_token_for_this_user)   # token 从 cache 拿
```

两种都实现 `credential.get_token()` 接口，对 MCP 看是一样的。**这是 runtime 侧的 auth 抽象，不是 MCP 侧的改动**。

---

## 9. 为什么砍了 OBO 还要验 JWT

这是我们讨论后期一个关键澄清。砍了 OBO 之后，user token 不再被下游 service 当凭证用，看起来好像"那我们不需要 user identity 了"。**错**。

### 9.1 Identity 的两种用途

| 用途 | 作为什么 | 例子 |
|---|---|---|
| **Credential** | 凭证 / token | 调 ADF 时附在 Authorization header |
| **Index** | 分桶 key | Cache 查找、SSE 路由、audit 记录 |

砍了 OBO = 只砍了 **credential** 这一路用法。**index** 用法还在，而且非常关键。

### 9.2 关键场景：Device Code Cache 必须按 user 分桶

Token cache 的 key 如果不含 user_id，会有严重 data leak：

```
T1: 用户 A 登 Snowflake → cache["snowflake"] = A 的 token
T2: 用户 B 请求进来 → cache hit → 用 A 的 token 调 Snowflake
    → B 看到了 A 的 Snowflake 数据
    → A 在 Snowflake 审计日志里背锅，B 实际操作
```

**必须** `cache[(user_id, provider)]` 才安全。所以 runtime **必须**知道当前请求是谁。

### 9.3 Service mode 下也需要 identity

即使下游 token 是 container 的 service credential，这些事还要 identity：

1. **SSE event routing**：一个 agent 有多个 provider 时（比如一个 Azure service + 一个 Snowflake device_code），中途触发 device flow 要把 URL 推给**对的那个用户的 SSE 连接**
2. **Admission control**：没 auth = 任何人能打到 `/api/chat`。service mode 只解决了"用什么身份调下游"，没解决"谁能用这个 agent"
3. **Session 归属**：`agent_frontend` 的 session 存储必须按 user 分，不然所有人看同一堆历史
4. **Audit / compliance**："某用户触发了某查询"是合规要求
5. **Rate limit / quota**：每人每分钟 N 次，没 user_id 做不了

### 9.4 为什么要**验** JWT，不是只**读** header

"读 header 拿 user_id" 和 "**可信地**拿到 user_id" 不是一回事。

- 读 `X-MS-CLIENT-PRINCIPAL-ID` header：任何能打到 container 的人都能伪造。用作 cache key → 攻击者塞别人的 user_id → cache 污染 → data leak。
- 验 `X-MS-TOKEN-AAD-ID-TOKEN` 的 JWT 签名（用 AAD 公钥 JWKS）：**密码学证明**这个 user_id 是 AAD 发的，伪造成本 = 拿到 AAD 私钥 = 不现实。

**JWT 验签的目的不是"为了拿下游 token"**（那是 OBO 的用法），**是"为了让 user_id 可信"**。user_id 可信了，cache key 才可信，分桶才可信，整套设计才不破防。

### 9.5 总结成一张表

砍 OBO 之后的 identity 需求：

| 用途 | 需要 identity 吗 | 原因 |
|---|---|---|
| 下游 credential | ❌ 已砍 | OBO 砍了，不从 user token 派生下游 token |
| Device code cache key | ✅ 必须 | 不分桶 → 跨用户 token 污染 → data leak |
| SSE device flow event 路由 | ✅ 必须 | URL 得推给触发的那个人 |
| Admission control | ✅ 必须 | 无 auth = 公开 endpoint |
| Audit / compliance | ✅ 必须 | 记录谁做了什么 |
| Rate limit / quota | ✅ 必须 | 按 user 限流 |

JWT 验签让上面这些 user_id **可信**。砍 OBO 并没有减少对 identity 的依赖，只是把它的用法从 "credential" 简化到 "index"。

### 9.6 能不能连 JWT 都不验

理论可以，前提：

- 网络层已经锁死（App Service Access Restriction + Private Endpoint），保证没经过 EasyAuth 的请求**物理上**打不到 container
- 只信任 EasyAuth 注入的 header
- 接受"以后要把 runtime 当 service 给别人调"时需要重写入口

MVP 想快可以这么做，等于把 threat model 推给 infra 层。**代码里读 header 后那一刻就要提取 user_id 存起来**，下游全用那个值。未来加 JWT 验签只改入口一处。

---

## 10. 最终架构

### 10.1 数据流图

```
User ──▶ EasyAuth ──▶ frontend
                        │
                        │ 从 X-MS-TOKEN-AAD-ID-TOKEN 拿 JWT
                        │ (只用来验身份，不用来调下游)
                        │
                        ▼
                   Authorization: Bearer <id_token>
                        │
                        ▼
                    runtime
                        │
                        │ ① 验 JWT (签名 + iss + aud + exp)
                        │    → UserIdentity → ContextVar
                        │
                        │ ② MCP tool 被调用
                        │    查 auth.json 看 provider 是哪种 mode
                        │
                        ├── mode: service      ──▶ 容器凭证 (MI / service account) ──▶ 下游
                        │                          不看 user
                        │
                        └── mode: device_code  ──▶ cache[(user, provider)]
                                                    ├─ hit  → 注入 ──▶ 下游
                                                    └─ miss → 触发 device flow ──▶ frontend SSE
                                                               用户在新 tab 登录 ──▶ token → cache ──▶ 重试
```

**核心观察**：

- **AAD access_token 不再穿过系统往下游走**。id_token 只在 runtime 入口用来验身份。
- **下游 credential 完全和 EasyAuth 解耦**：要么来自 container MI，要么来自 device flow cache。两条路都不依赖 EasyAuth 的 token。
- **identity 作为 cache 分桶 key**，贯穿整个请求生命周期（ContextVar）。

### 10.2 Module 结构

强烈推荐 auth 独立成 **top-level package**，和 `agent_runtime` / `agent_frontend` / `agents` 并列：

```
agent-runtime/
├── agent_frontend/          ← 用 auth 解 EasyAuth header、验 JWT、转发 Bearer
├── agent_runtime/           ← 用 auth 验 JWT、注入 token 到 MCP 调用
├── auth/                    ← ★ 新建，纯逻辑，不依赖 runtime/frontend
│   ├── __init__.py
│   ├── identity.py          ← UserIdentity dataclass、JWT 验签
│   ├── easyauth.py          ← 解 X-MS-* headers → UserIdentity
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py          ← CredentialProvider 抽象
│   │   ├── azure_service.py ← Managed Identity mode
│   │   ├── azure_device.py  ← Azure device code flow
│   │   ├── snowflake_device.py
│   │   └── ado_device.py
│   ├── cache.py             ← dict[(user_id, provider), TokenRecord] + Lock
│   └── config.py            ← 加载 auth.json
└── agents/
    └── adf-agent/
        └── settings/
            ├── mcp.json
            ├── HITL.json
            └── auth.json    ← ★ 新文件，每个 agent 一份
```

**为什么独立包而不是塞进 runtime**：

1. **frontend 也要用**（解 EasyAuth header），两边都 import 同一份避免重复
2. **纯函数逻辑**，单独 pytest 不用起 FastAPI
3. **可替换**：未来换 OIDC / Okta，改一个包，不动 runtime
4. 符合"freeze runtime"原则 —— runtime 不该因为加个 provider 就改业务代码

### 10.3 auth.json Schema

每个 agent 在 `settings/auth.json` 里声明：

```json
{
  "providers": {
    "azure-privileged": {
      "mode": "device_code",
      "authority": "https://login.microsoftonline.com/{tenant}",
      "scope": "https://management.azure.com/.default",
      "client_id": "..."
    },
    "snowflake": {
      "mode": "device_code",
      "account": "xy12345.us-east-1",
      "scope": "session:role:ANALYST"
    },
    "ado": {
      "mode": "device_code",
      "org": "mycompany"
    }
  },
  "mcp_bindings": {
    "adf":       "azure",
    "snowflake": "snowflake"
  }
}
```

**约定**：

- `mode` 只有两个值：`"service"` 或 `"device_code"`
- **`providers` 里不写某项 = 该 provider 默认 service mode**。例如上面没写 `"azure"`，表示 ADF MCP 用 container 的 Managed Identity。
- Azure 的 `service` mode → `DefaultAzureCredential()`
- Snowflake 的 `service` mode → 容器里的 service account 凭证（key-pair / client credentials from env）
- 所有 `device_code` mode → 统一走 device flow primitive
- `mcp_bindings` 告诉 runtime 每个 MCP server 默认用哪个 provider

**切号场景**（用户想用特权账号）：runtime 提供一个 tool `switch_identity(provider="azure-privileged")`，用户显式触发，后续 ADF 调用改用 `azure-privileged` 的 token 覆盖默认。

### 10.4 三层威胁防御

按投入排序：

1. **必做 —— 应用层 JWT 验签**（`auth/identity.py::validate_jwt`）
   - 有这一层，就算别人绕过 EasyAuth 直接打 container 也进不来
   - **逻辑上**的保护

2. **强烈建议 —— 网络层限制**
   - App Service Access Restrictions
   - Private Endpoint
   - VNet Integration
   - 限制 ingress 只接受 Azure Front Door / 特定 VNet / 特定 IP
   - **物理上**的 defense in depth

3. **可选 —— "Require Auth" 模式**
   - EasyAuth 的 "Require authentication" 开关：未登录直接 302 到 AAD，不会进你的 app
   - 对 UI 路径是硬门禁
   - Service caller 路径可以关掉或 pass-through

---

## 11. 具体改动清单

### 11.1 新增文件

| 文件 | 职责 |
|---|---|
| `auth/identity.py` | `UserIdentity` dataclass、`validate_jwt(token) -> UserIdentity` |
| `auth/easyauth.py` | `parse_easyauth_headers(headers) -> UserIdentity`（参考 `../generic-ai/opsagent2/flask_app.py:134-193`，但重写） |
| `auth/cache.py` | `dict[(user_id, provider), TokenRecord]` + `threading.Lock`，`get/put/invalidate` |
| `auth/providers/base.py` | `CredentialProvider` 抽象（`get_credential(user_id) -> Credential`） |
| `auth/providers/azure_service.py` | 返回 `DefaultAzureCredential()` |
| `auth/providers/azure_device.py` | Azure device code state machine |
| `auth/providers/snowflake_device.py` | Snowflake device code |
| `auth/providers/ado_device.py` | ADO device code |
| `auth/config.py` | `load_auth_config(path) -> AuthConfig` |
| `agents/adf-agent/settings/auth.json` | 每个 agent 一份，声明式 |

### 11.2 修改文件

| 文件 | 改什么 |
|---|---|
| `agent_frontend/server.py:46` | 删 `_DEFAULT_USER_ID`，路由加 `user: UserIdentity = Depends(require_user)` |
| `agent_frontend/server.py:225` | chat proxy 转发时加 `Authorization: Bearer <id_token>` header |
| `agent_runtime/api/routes/chat.py` | 加 auth dependency：验 JWT → set ContextVar |
| `agent_runtime/api/routes/confirm.py` | 同上（HITL 也要知道是谁） |
| `agent_runtime/api/routes/meta.py` | 同上（可考虑 `/api/healthz` 不要 auth） |
| `agent_runtime/engine.py:316-408` | `chat_stream` 接收 `UserIdentity`，传给 `_run_sync`；**ContextVar 跨 ThreadPoolExecutor 边界要显式 propagate** |
| `agent_runtime/core/tools.py` | MCP tool call 前 middleware：读 ContextVar 拿 user → 查 auth config 找 provider → `auth.get_token(user, provider)` → 注入 token 进 args（device mode）或让 MCP 用自己的 service credential |
| `agent_runtime/core/config.py` | 新增 `resolve_auth_config()` 读 `auth.json` |
| `agent_runtime/engine.py::_ConfirmRegistry` | 泛化或 parallel：device flow 事件用同构的 pending-action 机制推给 frontend（复用现有 HITL plumbing） |
| `agents/adf-agent/mcp/adf_mcp_server.py:26-35` | 删掉 `_client = DefaultAzureCredential()`，改 `ContextualCredential` + thread-local token |
| `agents/adf-agent/mcp/adf_mcp_server.py` 各 tool | 每个 `@mcp.tool` 加隐式 `_auth_token` 参数（用装饰器批量处理避免手写每个） |
| `agents/base.Dockerfile` | 把 `auth/` 包也 COPY 进 container |

### 11.3 保持不变

- `skills/`、`prompts/system.md` — 业务层，不碰
- `agents/adf-agent/settings/mcp.json`、`HITL.json` — 独立配置
- HITL primitive 本身 — 只是被 device flow 借用同构模式

### 11.4 关键注意点：ContextVar 跨线程

`engine.py:394` 有 `loop.run_in_executor(self._executor, _run_sync)`。**ContextVar 是线程本地，不会自动跨到 executor 线程**。

需要：

```python
import contextvars
ctx = contextvars.copy_context()
future = loop.run_in_executor(self._executor, lambda: ctx.run(_run_sync))
```

或者在 `_run_sync` 开头把 user_id 再 set 一次。这个踩坑率 100%，第一个 PR 就要做对。

---

## 12. MVP 边界

### 12.1 MVP 范围（v1）

1. `auth/` 包（无 OBO）
2. JWT 身份验证（验了但不用作下游凭证）
3. Service credential provider（复用现有 `DefaultAzureCredential` 逻辑）
4. Azure / Snowflake / ADO 三个 device code provider
5. Token cache（进程内 dict + Lock）
6. `auth.json` schema + 加载
7. MCP `ContextualCredential` 改造（ADF agent 先行）
8. engine ContextVar 跨线程 propagate
9. Pending-action SSE event（device code URL 推给 frontend，复用 HITL 机制）

### 12.2 v1 明确**不做**

- OBO flow（搁置到触发条件出现）
- Option B audience（runtime 独立 AAD app，搁置到 runtime 直接对外暴露时）
- 跨 container / 跨 worker 的 token 共享（Redis / Key Vault store）
- Token 持久化（container 重启后需重登，可接受）
- Refresh token 管理（device code 拿到的 token 过期就重走 device flow）
- 多 tenant 支持（单 tenant 硬编码 in config）

### 12.3 升级路径

| 触发条件 | 升级动作 |
|---|---|
| Device code UX 对常用用户成为负担 | 加 `mode: "obo"`，作为 Azure 的可选优化 |
| Runtime 直接对外暴露给其他 service | 升 Option B：runtime 独立 AAD app + frontend 做 OBO 转 audience |
| Scale out 到多实例 | Token cache 从 dict 换 Redis |
| 合规要求 token 持久化 | Token cache 从 in-memory 换 Key Vault |
| 多 frontend 共用 runtime | Option B 必做 |

---

## 13. Open Questions / 待决策

开工前需要先拍板的几件事：

1. **auth 放 top-level 还是 `agent_runtime/core/auth.py`？**
   - 推荐 top-level，理由见 §10.2。
   - 简化选项：先放 `agent_runtime/core/auth.py`，后续抽出。

2. **JWT 验签 MVP 是否要做？**
   - 要做：安全 baseline 正确，未来 runtime 对外暴露零改动
   - 不做：依赖网络层保护 header 不可伪造，以后对外时要重写入口
   - 推荐：做，不贵

3. **Switch identity UX 显式还是隐式？**
   - 显式：一个 tool `switch_identity(provider="azure-privileged")`，用户主动触发
   - 隐式：agent 根据 tool 需求自动选 provider，必要时弹 device flow
   - 推荐：显式。隐式切号容易让用户搞混"我现在用哪个身份"。

4. **`mcp_bindings` 字段是否必要？**
   - 必要：用户可以在 agent 层覆盖 MCP 默认 provider
   - 不必要：MCP server 自己声明用哪个 provider（代码里写死）
   - 推荐：必要。灵活性几乎零成本。

5. **MVP 阶段 runtime 的 Bearer audience 策略？**
   - Option A（共用 AAD app）：简单，MVP 推荐
   - Option B（runtime 独立 AAD app + frontend OBO 转 audience）：严格
   - 推荐：A，代码结构留好升 B 的口子（AUDIENCE env var）

---

## 14. 术语对照表

| 术语 | 含义 |
|---|---|
| **EasyAuth** | Azure App Service 自带的 SSO middleware，帮你做完 OAuth2 authorization code flow |
| **AAD App Registration** | Azure AD 里的一条记录，描述"有这么一个 app"，给它 client_id 和 secret |
| **client_id** | App 的身份证号 |
| **client_secret** | App 向 AAD 证明自己是那个 app 的密码 |
| **audience (`aud`)** | Token 上的 claim，标示"这张 token 给哪个服务用" |
| **scope** | 请求 token 时说"我要访问哪个资源的什么权限"，AAD 会把它编码进 token 的 aud |
| **id_token** | OIDC 规定的 JWT，表达"这个用户是谁"，给 app 读的，不给下游用 |
| **access_token** | OAuth2 规定的 token，给 app 拿去调下游 API 的 |
| **refresh_token** | 过期重取 access_token 用的长期凭证，不直接调 API |
| **OBO (On-Behalf-Of)** | 用一张 token 去 AAD 换另一张 audience 不同但身份相同的新 token |
| **Managed Identity (MI)** | Azure 给你的 resource（VM / App Service / Container）自动分配的 AAD 身份，不用你管 secret |
| **Service Principal (SP)** | AAD 里代表 "一个 app / service" 的身份实体，是 App Registration 的运行时体现 |
| **Device Code Flow** | 一种 OAuth2 flow：app 拿 device_code，用户拿 user_code 到另一个浏览器登录，app 轮询拿 token |
| **UserIdentity** | 本项目定义的 dataclass，封装从 header / JWT 解出的用户信息 |
| **Provider** | 本项目定义的概念，一个身份源（azure / snowflake / ado），每个 provider 有自己的 mode 和参数 |
| **Mode** | 一个 provider 怎么拿 token，二选一：`service` / `device_code` |

---

## 15. 参考设计来源

- `../generic-ai/opsagent2/flask_app.py:134-193` 的 `get_user_info()` —— EasyAuth header 解析样板。**可复用解析逻辑**（base64 decode claims + 扫 typ=="name"），**不可复用架构**（和 storage mode 耦合、散落调用、吞异常）。
- Azure AD docs "On-Behalf-Of flow"：https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow
- EasyAuth token store docs：https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-oauth-tokens
- MSAL Python device code sample：https://github.com/AzureAD/microsoft-authentication-library-for-python

---

## 附录 A —— 决策时间线（给未来接手人）

为了便于未来人理解"为什么是这样"，记录一下我们讨论的几个关键转折点：

1. **最初方案**：EasyAuth → 转发 access_token 到 runtime → runtime 做 OBO → 传给 MCP。
2. **发现 MCP 固化身份问题**：`_client` singleton 导致跨用户 token 污染 → 提出 `ContextualCredential` 模式。
3. **Audience 策略讨论**：Option A（共用 app）vs Option B（独立 app + frontend OBO）。MVP 决定走 A。
4. **用户提议**：能不能不用 OBO，只用 Managed Identity + Device Code？
5. **发现**：Device Code 能覆盖 OBO 的所有场景（除了 UX 平滑度），Managed Identity 覆盖"不需要 per-user authZ"的常见场景。
6. **决定砍 OBO**：MVP 只做 `service` / `device_code` 两种 mode。OBO 留作未来 UX 优化。
7. **澄清**：砍 OBO 不等于不要 identity。Identity 从 credential 降级成 index（cache 分桶、SSE 路由、audit），仍然关键，所以 JWT 验签仍要做。
8. **最终架构**：`auth/` top-level 包 + `auth.json` 声明式配置 + `ContextualCredential` MCP 改造 + JWT 验签入口。

这份文档记录的就是上面第 8 步的定稿。
