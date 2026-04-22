# Auth v1 Implementation Status

> Scope: the five-PR series implementing the multi-user SSO + container auth
> design. For the full design rationale see `doc/auth-flow.md`.

---

## 1. What v1 delivers (scope of PR1–PR5)

### Identity layer
- EasyAuth header parsing in `agent_frontend` — extracts user identity from
  `X-MS-CLIENT-PRINCIPAL-*` and the AAD ID token.
- JWT signature validation in `agent_runtime` using Azure AD JWKS —
  verifies `iss`, `aud`, `exp`, and that `scp` contains the required scope.
- `UserIdentity` dataclass as the canonical in-process representation.
- `ContextVar` propagation through the request lifecycle, including across
  the `ThreadPoolExecutor` boundary in `engine.chat_stream`.
- Dev-mode bypass gated behind `AUTH_DEV_MODE=1` (decoupled from storage
  mode, unlike the `opsagent2` reference implementation).

### Credential layer
- Top-level `auth/` package, independent of `agent_runtime` and
  `agent_frontend`, so it can be consumed by any future component.
- Two credential modes per provider:
  - `service` — container's Managed Identity (Azure) or container-held
    service account. Shared across all users; token cache key has no
    `user_id` component.
  - `device_code` — user completes an OAuth2 device authorization grant.
    Token cached per `(user_id, provider)`.
- Provider implementations shipped in v1: `azure_service`, `azure_device`,
  `snowflake_device`, `ado_device`.
- In-process token cache (`dict` + `threading.Lock`) with per-user keys.

### MCP decoupling
- `adf_mcp_server.py` singleton `DefaultAzureCredential` removed.
- `ContextualCredential` pattern: one long-lived `DataFactoryManagementClient`
  per subprocess; credential's `get_token()` reads from a `threading.local`
  that the runtime populates before each tool call.
- MCP tools accept an implicit `_auth_token` argument (decorator injected,
  not visible to the LLM tool schema).
- Runtime-side middleware looks up the MCP's bound provider in `auth.json`,
  fetches the token, and injects it into tool args.

### Device flow UX
- Device code state machine reuses the existing HITL pending-action
  primitive (`_ConfirmRegistry` in `engine.py`).
- SSE event type added (sibling of `confirm_request`) carrying the
  verification URL and user code for the frontend to display.
- Per-user, per-provider flow isolation: one user's device login does
  not block or leak into another user's session.

### Configuration
- `agents/<name>/settings/auth.json` per-agent declaration:
  - `runtime.audience` and `runtime.required_scope` for JWT validation.
  - `providers.*` defining which credential modes are available.
  - `mcp_bindings` mapping MCP server names to provider names.
- `agents/base.Dockerfile` updated to include the `auth/` package.

### AAD setup (deployment-side, not code)
- One AAD app registration for the whole agent runtime fleet.
- One exposed scope: `runtime.access`.
- All agent containers validate against the same `audience` and
  `required_scope`.

---

## 2. What v1 does NOT do (explicit exclusions)

### Auth flows deferred
- **On-Behalf-Of (OBO) flow.** Downstream Azure calls never exchange the
  user's EasyAuth token for a downstream-scoped token. If per-user Azure
  access is needed, the user runs a Device Code login against Azure.
- **Multi-scope authZ.** All runtimes share one scope. No per-runtime
  access differentiation at the AAD layer.
- **Separate AAD apps per runtime.** Every runtime shares one app
  registration and one client_secret.
- **Client credentials / app-only tokens.** Runtime only accepts
  user-delegated tokens in v1.

### Operational features deferred
- **External token store.** No Redis, Key Vault, or database-backed cache.
  Tokens live in the uvicorn worker's memory and die with the process.
- **Multi-instance cache coherence.** If App Service scales out,
  per-instance caches diverge. Mitigation: sticky sessions at the
  ingress layer.
- **Token auto-refresh.** Device code flows return a `refresh_token` but
  v1 does not use it. Expired tokens trigger a new device login.
- **Token revocation on logout.** No explicit logout endpoint, no cache
  invalidation on session end.
- **Per-user admission control.** Auth answers "is this a valid user?"
  not "is this user allowed to use this specific agent?".
- **Rate limiting.** No per-user request throttling.

### Hardening deferred
- **JWT signing key rotation edge cases.** Relies on `PyJWKClient`'s
  default cache behavior; no explicit rotation drill or failure handling.
- **Structured audit logging.** Identity is available for logs but v1
  does not standardize an audit event schema.
- **Client secret rotation automation.** Manual rotation via Azure portal.
- **Security scanning** of the `auth/` package dependencies beyond what
  existing CI does.

---

## 3. Future work (post-v1, prioritized roughly by expected need)

### Likely needed within 1–2 releases
1. **Multi-scope authZ** — split `runtime.access` into per-runtime scopes
   (`runtime.adf`, `runtime.snowflake`, ...) once the fleet grows to >3
   distinct trust domains. Implementation is one `auth.json` field change
   per agent plus AAD portal config.
2. **Token auto-refresh** — use `refresh_token` to avoid forcing users
   through device code every ~1 hour.
3. **Audit event schema** — structured log line per tool call:
   `{user_id, agent, tool, provider, token_source, timestamp}`. Feeds
   into SIEM downstream.
4. **Rate limiting** — per-user request cap on `/api/chat`, simple
   in-memory counter initially.

### Likely needed at scale
5. **External token store** — Redis-backed `TokenCache` implementation
   behind the existing interface. Enables multi-instance deployments
   without sticky sessions.
6. **OBO provider** — add `mode: "obo"` as a third option alongside
   `service` and `device_code`. Removes the device-code UX tax for
   same-tenant Azure access. New file: `auth/providers/azure_obo.py`.
7. **Per-runtime AAD apps** — when a runtime crosses a trust domain
   (external consumers, stricter compliance), promote it to its own AAD
   app registration. Only that runtime's `auth.json` changes.
8. **Per-user admission control** — allowlist/groups that determine which
   agents a user can access. Checked at `/api/sessions` and `/api/chat`.

### Nice to have eventually
9. **Additional IdPs** — Okta, Google Workspace, etc. The provider
   abstraction accommodates this; add new files under `auth/providers/`.
10. **Token revocation endpoint** — explicit `/api/logout` that evicts
    the user's entries from the cache.
11. **Device flow abandonment cleanup** — GC orphaned pending flows
    after a timeout.
12. **Key Vault for client secrets** — replace env var with KV reference
    (Azure Container Apps / App Service both support this natively).

---

## 4. Production readiness gap

What v1 delivers is **functionally complete and safe for trusted internal
users**. It is **not** production-ready for external-facing deployment or
compliance-sensitive workloads. The gaps, grouped by blocker severity:

### Must-fix before any production rollout
- **Network layer hardening.** App Service Access Restrictions must be
  configured so the runtime container cannot be reached bypassing
  EasyAuth. Without this, an attacker on the VNet can forge EasyAuth
  headers. (Infra change, not code.)
- **client_secret in Key Vault.** v1 reads from env var; production
  should read from Key Vault via managed identity. Leaked env vars in
  logs or process listings are the #1 secret exfiltration path.
- **Error messages scrubbed.** Audit every `HTTPException` and log line
  in the auth path to ensure no JWT fragment, no token, and no
  internal URL leaks to the client. v1 has reasonable defaults but
  no systematic review.
- **Integration tests.** Real EasyAuth header mock + real AAD JWKS
  validation path. v1 development tests are unit-level only.

### Must-fix for >1 production user / team
- **Rate limiting on `/api/chat`.** Without it, a single user can
  exhaust the thread pool or burn through the token quota. Even a
  trivial per-user counter (N requests/minute) closes the worst case.
- **Structured logging with correlation IDs.** Tie every log line to
  a `(user_id, trace_id, tool)` triple. v1 has the data available
  but no consistent emission schema.
- **Metrics.** At minimum: auth success/failure rate, device-flow
  completion rate, token cache hit rate, JWT validation latency.
  Feeds alerts on anomalies.
- **Dev bypass gating audit.** Ensure `AUTH_DEV_MODE` cannot be
  accidentally enabled in production (check default, CI, and
  deployment pipeline).

### Must-fix for compliance-regulated workloads (SOC2, ISO 27001, etc.)
- **Audit log export.** Standardized audit event per tool call and per
  auth event, shipped to SIEM.
- **Token retention policy.** Document how long tokens live in cache,
  when they are evicted, whether they persist across restarts.
- **Data residency.** If users are in multiple regions, cache location
  matters (user A's EU token must not end up in a US-region cache).
- **Third-party security review** of the `auth/` package.
- **Penetration test** including header forgery, token replay, JWT
  algorithm confusion, device-code phishing surface.

### Must-fix for horizontally scaled deployment
- **External token store.** In-memory dict per worker does not survive
  a restart and does not share across replicas. Sticky sessions work
  for <10 replicas; beyond that, Redis is the standard answer.
- **Graceful AAD outage handling.** JWKS endpoint and device-code
  polling endpoint are both dependencies. v1 has no circuit breaker,
  retry budget, or degradation path.
- **Cache size limits.** Unbounded `dict[(user_id, provider), ...]`.
  At 10k users × 3 providers, you're fine; at 1M users the cache
  needs LRU eviction and a memory ceiling.

### Would-be-nice for maturity
- **Chaos tests** — AAD is slow, AAD is down, MCP subprocess dies
  mid-tool-call, ContextVar leaks across requests (this one is the
  subtle one worth an explicit test).
- **Performance tests** — concurrent users, cache contention,
  JWT validation throughput.
- **Runbook** for common auth failures (device code expired, token
  refresh failed, JWT validation failed due to clock skew, etc.).

### Distance summary

| Deployment target | Blocker items from list above |
|---|---|
| Dev / staging, <10 trusted users | 0 — v1 is ready |
| Internal production, single team | Must-fix #1–4 + #5–8 |
| Internal production, multi-team | + #9–11 + rate limiting tier |
| External or regulated production | + #12–16 + security review |
| Horizontally scaled (multi-instance) | + #17–19 |

The pattern: v1 is the right **floor**. Each tier above adds cross-cutting
concerns that don't belong in the auth module itself — they belong in
ingress/observability/ops/compliance layers that the auth module
cooperates with. None of them require rewriting v1's design.

---

## Appendix: things that are intentional, not missing

Sometimes simplicity looks like an oversight. These are deliberate:

- **Tokens held in memory, not persisted.** Process restart = re-login.
  This is a feature for session cleanliness; if it's ever a UX
  problem, add an external store, not disk persistence.
- **No user-visible list of "what auth I have".** A tool call failing
  due to missing auth triggers device flow on demand. Surfacing auth
  state as a UI affordance is a frontend concern, not a runtime one.
- **MCP subprocesses are shared across users.** Safe because they hold
  no user state after the `ContextualCredential` refactor.
- **Service mode caches per-container, not per-user.** This is the whole
  point of service mode; keying it by user would be wrong.
