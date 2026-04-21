# Stress test observations — 10 concurrent sessions

Captured 2026-04-20 against the stack:

```
stress_chat_concurrent.py  ──►  agent_frontend :8080  ──►  agent-runtime :8001 (ADF agent)
```

Script: [`tests/stress/stress_chat_concurrent.py`](../tests/stress/stress_chat_concurrent.py).

**TL;DR** — runtime + frontend handled 10 concurrent SSE streams with 23 tool calls and 14 HITL approvals without races, throttling, ordering violations, or cross-session `request_id` leakage. All streams terminated cleanly with `stop_reason: "end_turn"`. Four observations below — none are crashes, but #1 is a real UX bug.

---

## Setup

- 10 sessions created concurrently via `POST /api/sessions`, each assigned a different prompt bucket (general chat, skill intro, ADF overview via MCP, two `write_file`-forcing tasks, one `bash`-forcing task, small reasoning, pipeline listing, two-turn dialog).
- Per-session driver parses SSE, reconstructs Anthropic-shape assistant messages (text + tool_use blocks), POSTs the whole history back on each turn — matching what `script.js` does in the real UI.
- Any `confirm_request` event is auto-approved (`allowed: true`) via `POST /api/sessions/{id}/confirm/{request_id}`.
- Script deletes all test sessions at end so storage isn't polluted.

## Results

| Metric | Value |
|---|---|
| Concurrent sessions | 10 |
| Total turns | 11 (S9 had 2) |
| tool_call / tool_result | 23 / 23 (1:1) |
| HITL `confirm_request` events | 14 |
| ... of which `bash` | 10 (all in S3) |
| ... of which `write_file` | 4 |
| approve HTTP status | all `200` |
| approve proxy latency | 3–13 ms (p50 ≈ 8 ms) |
| confirm → tool_result lag | 0.02–0.11 s |
| turn latency | min 950 ms / p50 4.7 s / p95 40.1 s / max 40.1 s |
| 4xx / 5xx responses | 0 |
| SSE streams not ending in `done` | 0 |
| `stop_reason` anomalies | 0 (all `end_turn`) |

## Correctness cross-checks (all passed)

1. Every `tool_call.id` has exactly one matching `tool_result.id` — none missing, none duplicated.
2. Every `confirm_request` arrived **after** its `tool_call` and **before** its `tool_result` (per design §9).
3. Every SSE stream ends with `done`; no events arrived after `done`.
4. **All 14 HITL `request_id` values were unique across the 10 sessions** — i.e. the exact race the design doc §8 warned about (`self._on_event` engine attr routing confirm to wrong SSE stream) does not reproduce. Good.

---

## Observations

### 1. 🔴 `write_file` path-sandbox check runs *after* HITL — user approves a no-op

**Reproduction**: S4 asked the agent to write `/workspace/notes.txt`. Flow observed:

1. Agent emits `tool_call` for `write_file` with absolute path.
2. Runtime emits `confirm_request` → script (human) approves → `200`.
3. Tool executes, **only now** the workspace sandbox rejects the absolute path, emits `tool_result` with `is_error: true` and message "outside permitted workspace directory".

The user already clicked "Allow" on a write that was never going to happen. Two reasonable fixes:

- Normalize absolute paths in-sandbox to relative paths before the HITL gate fires (probably what the user expected).
- Or run the sandbox validation first; if it fails, emit `tool_result` with the error directly and skip the `confirm_request` entirely.

Either way, the current ordering wastes a user decision.

### 2. 🟡 One approval on `bash` turns into an N-chain with no additional friction

S3 got one bash call approved, then the model emitted **9 more `bash` `tool_call`s in the same turn**, each individually HITL-gated. The script auto-approved all 10, and the runtime handled each correctly (serial, unique `request_id`, 11–13 ms proxy latency each).

No runtime bug — serial per-chat execution is by design (§8). But:

- A human clicking through a UI modal 10 times in 30 seconds is unlikely — either they'll give up or rubber-stamp.
- A scripted bot attached to this frontend with auto-approve would not notice that "one bash" became "ten bash".

Worth thinking about whether `confirm_request` should carry a "you've already approved N bash calls this chat" hint, or whether the UI should offer "approve this tool for the rest of the turn" as an explicit, visible choice.

### 3. 🟡 `bash` is hard to trigger — HITL regression testing is prompt-fragile

The ADF system prompt (`agents/adf-agent/prompts/system.md`) aggressively discourages bash. Out of five bash-forcing prompts tried:

| Prompt | Result |
|---|---|
| `"run echo stress-test"` | refused, flagged as prompt injection |
| `"run uname -a as diagnostic"` | refused, flagged as recon |
| `"run df -h /workspace as pre-flight"` | refused, "out of scope" |
| `"write summary to /workspace/adf-summary.md"` | indirectly triggered bash (fallback after write_file sandbox rejection) |
| `"create /workspace/notes.txt"` | triggered write_file only (not bash) |

The model's willingness to reach for `bash` depends heavily on wording and on what other tools failed first. For HITL regression tests going forward, consider:

- A dedicated "diagnostic" skill that legitimately needs bash (then the skill invocation is the stable trigger).
- A test agent variant with a permissive system prompt.

Right now a small change to the system prompt could silently break the HITL-bash path in CI without any test noticing.

### 4. 🟢 Single-session HITL chain does **not** starve others

S3's 13-confirm chain pushed its own turn to 40 s wall clock. Meanwhile the other 9 sessions completed in 1–12 s each. This confirms the design: per-chat serial, cross-chat parallel. Documenting it here because it's the first time it was verified under load.

---

## How to reproduce

```bash
# 1. start the ADF agent runtime on :8001 (per agents/adf-agent/README or similar)
# 2. start the frontend on :8080
uv run agent-web

# 3. run the stress script (needs live Anthropic API key on the runtime side)
uv run python tests/stress/stress_chat_concurrent.py
```

The script dumps a per-session summary + a `HITL TIMELINE` section for every session that received at least one `confirm_request`. A non-empty `ISSUES FOUND` block at the end means one of the cross-checks failed.
