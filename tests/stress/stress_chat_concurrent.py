"""Concurrent stress test for agent_frontend + agent-runtime.

Drives 10 sessions in parallel through the frontend on :8080, auto-approves
any HITL `confirm_request` events, and cross-checks SSE stream correctness:

  - 1:1 pairing of tool_call / tool_result by id
  - confirm_request lands between its tool_call and tool_result
  - done is the last event of each turn
  - confirm request_ids are unique across sessions (no cross-session routing)
  - every /api/confirm POST returns 200

This is NOT a pytest test. It:
  - requires a live agent_frontend on :8080 AND a live agent-runtime on :8001
  - makes real LLM calls (costs real tokens)
  - takes ~20–40 seconds wall clock

Run manually:
    uv run python tests/stress/stress_chat_concurrent.py

The prompt bucket is tuned for the ADF agent. If you point this at a
different agent, expect different tool-call / HITL patterns — edit
USER_PROMPTS accordingly.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from collections import Counter, defaultdict
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

FRONTEND = "http://localhost:8080"

USER_PROMPTS: List[List[str]] = [
    # 0 — general chat
    ["Hi! In one short sentence, what is your job?"],
    # 1 — skills intro (tests /api/skills meta path indirectly via prompt)
    ["What skills do you have available?"],
    # 2 — ADF overview (multi MCP tool_calls, no HITL)
    ["Give me a one-paragraph summary of this Azure Data Factory environment."],
    # 3 — write_file HITL; ADF system prompt allows workspace scratch writes
    [
        "Please save a short ADF-overview memo to the workspace at `/workspace/adf-summary.md` "
        "with the text:\n\n# ADF summary\n- Pipelines: (count)\n- Data flows: (count)\n- Linked services: (count)\n\n"
        "Use your write_file tool. Use the literal placeholders if you don't want to call MCP first."
    ],
    # 4 — write_file HITL #2
    [
        "Please create a file at `/workspace/notes.txt` containing the single line "
        "`stress-test note` using your write_file tool. This is a workspace scratch artifact."
    ],
    # 5 — bash HITL (hard to trigger; see doc/stress-test-observations.md §3)
    [
        "Before I ask you to export a large report, I need to see free disk space in the workspace. "
        "Please run `df -h /workspace` with your bash tool and show me the raw output. "
        "This is a legitimate pre-flight workspace check — write_file has no way to give me this."
    ],
    # 6 — small reasoning, no tools
    ["What is 17 * 23? Just the number."],
    # 7 — MCP pipelines (no HITL)
    ["List up to 3 pipelines you can see in ADF."],
    # 8 — MCP linked services (no HITL)
    ["How many linked services are configured? Just the count."],
    # 9 — two-turn dialog
    ["Say the word 'apple'.", "Now say 'banana'."],
]

# ─── SSE parsing ──────────────────────────────────────────────────────────


async def iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, Dict[str, Any]]]:
    """Yield (event, data) pairs from an SSE response stream."""
    current_event: Optional[str] = None
    data_buf: List[str] = []
    async for line in resp.aiter_lines():
        if line == "":
            if current_event is not None:
                raw = "\n".join(data_buf)
                try:
                    obj = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    obj = {"_raw": raw}
                yield current_event, obj
            current_event = None
            data_buf = []
            continue
        if line.startswith(":"):
            continue  # SSE comment / keepalive
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_buf.append(line[len("data:"):].lstrip())


# ─── Per-session driver ───────────────────────────────────────────────────


class SessionRun:
    def __init__(self, idx: int, prompts: List[str], agent_url: str, agent_name: str):
        self.idx = idx
        self.prompts = prompts
        self.agent_url = agent_url
        self.agent_name = agent_name
        self.session_id: Optional[str] = None
        self.events: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.tool_calls: List[Dict[str, Any]] = []
        self.tool_results: List[Dict[str, Any]] = []
        self.confirms_seen: List[Dict[str, Any]] = []
        self.confirms_approved: List[Dict[str, Any]] = []
        self.done_reasons: List[str] = []
        self.turn_latencies_ms: List[float] = []

    async def create(self, client: httpx.AsyncClient, t0: float) -> None:
        r = await client.post(
            f"{FRONTEND}/api/sessions",
            json={"agent_url": self.agent_url, "agent_name": self.agent_name},
            timeout=10.0,
        )
        r.raise_for_status()
        self.session_id = r.json()["id"]
        self.events.append(
            {"t_rel": time.monotonic() - t0, "turn": -1, "event": "_created", "data": self.session_id}
        )

    async def run_turn(
        self,
        client: httpx.AsyncClient,
        turn: int,
        messages: List[Dict[str, Any]],
        t0: float,
    ) -> Dict[str, Any]:
        turn_start = time.monotonic()
        trace_id = f"t{self.idx}-{turn}-{int(time.time()*1000)}"

        # State we're rebuilding from the event stream (mirrors script.js).
        text_parts: List[str] = []
        current_text: List[str] = []
        tool_use_blocks: List[Dict[str, Any]] = []
        tool_results_for_next_turn: List[Dict[str, Any]] = []
        stop_reason: Optional[str] = None

        async def approve(request_id: str, tool_name: str) -> None:
            approve_start = time.monotonic()
            try:
                r = await client.post(
                    f"{FRONTEND}/api/sessions/{self.session_id}/confirm/{request_id}",
                    json={"allowed": True},
                    timeout=10.0,
                )
                self.confirms_approved.append({
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "turn": turn,
                    "status": r.status_code,
                    "latency_ms": (time.monotonic() - approve_start) * 1000,
                })
                if r.status_code != 200:
                    self.errors.append(
                        f"turn{turn}: confirm {request_id} for {tool_name} returned "
                        f"{r.status_code}: {r.text[:200]}"
                    )
            except Exception as e:
                self.errors.append(f"turn{turn}: confirm POST failed: {e}")

        pending_confirm_tasks: List[asyncio.Task] = []

        async with client.stream(
            "POST",
            f"{FRONTEND}/api/sessions/{self.session_id}/chat",
            json={"messages": messages, "trace_id": trace_id},
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(None, connect=10.0),
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                self.errors.append(
                    f"turn{turn}: /chat returned {resp.status_code}: {body[:300]}"
                )
                return {"assistant": None, "tool_results": [], "stop_reason": f"http_{resp.status_code}"}

            async for event, data in iter_sse(resp):
                self.events.append({"t_rel": time.monotonic() - t0, "turn": turn, "event": event, "data": data})

                if event == "text_delta":
                    current_text.append(data.get("text", ""))
                elif event == "text_stop":
                    text_parts.append("".join(current_text))
                    current_text = []
                elif event == "tool_call":
                    tool_use_blocks.append({
                        "type": "tool_use",
                        "id": data.get("id"),
                        "name": data.get("name"),
                        "input": data.get("args", {}),
                    })
                    self.tool_calls.append({"id": data.get("id"), "name": data.get("name"), "turn": turn})
                elif event == "tool_result":
                    tool_results_for_next_turn.append({
                        "type": "tool_result",
                        "tool_use_id": data.get("id"),
                        "content": data.get("output", ""),
                        "is_error": bool(data.get("is_error", False)),
                    })
                    self.tool_results.append({
                        "id": data.get("id"),
                        "name": data.get("name"),
                        "is_error": bool(data.get("is_error", False)),
                        "turn": turn,
                    })
                elif event == "confirm_request":
                    req_id = data.get("request_id")
                    tool_name = data.get("tool_name")
                    self.confirms_seen.append({"request_id": req_id, "tool_name": tool_name, "turn": turn})
                    if req_id:
                        pending_confirm_tasks.append(asyncio.create_task(approve(req_id, tool_name)))
                elif event == "done":
                    stop_reason = data.get("stop_reason")
                    self.done_reasons.append(stop_reason or "?")
                    break
                elif event == "error":
                    self.errors.append(f"turn{turn}: SSE error event: {data}")
                    stop_reason = "error"
                    break

        for t in pending_confirm_tasks:
            try:
                await t
            except Exception as e:
                self.errors.append(f"turn{turn}: approve task exception: {e}")

        self.turn_latencies_ms.append((time.monotonic() - turn_start) * 1000)

        assistant_content: List[Dict[str, Any]] = []
        for t_block in text_parts:
            if t_block:
                assistant_content.append({"type": "text", "text": t_block})
        if current_text:
            assistant_content.append({"type": "text", "text": "".join(current_text)})
        assistant_content.extend(tool_use_blocks)
        assistant_msg = {"role": "assistant", "content": assistant_content} if assistant_content else None

        return {"assistant": assistant_msg, "tool_results": tool_results_for_next_turn, "stop_reason": stop_reason}

    async def drive(self, client: httpx.AsyncClient, t0: float) -> None:
        messages: List[Dict[str, Any]] = []
        try:
            await self.create(client, t0)
        except Exception as e:
            self.errors.append(f"create failed: {e}\n{traceback.format_exc()}")
            return

        for turn_idx, prompt in enumerate(self.prompts):
            messages.append({"role": "user", "content": prompt})
            try:
                result = await self.run_turn(client, turn_idx, messages, t0)
            except Exception as e:
                self.errors.append(f"turn{turn_idx} exception: {e}\n{traceback.format_exc()}")
                break
            if result["assistant"] is not None:
                messages.append(result["assistant"])
            if result["tool_results"]:
                messages.append({"role": "user", "content": result["tool_results"]})


# ─── Orchestrator ─────────────────────────────────────────────────────────


async def main() -> int:
    t0 = time.monotonic()

    async with httpx.AsyncClient() as probe:
        r = await probe.get(f"{FRONTEND}/api/agents", timeout=5.0)
        r.raise_for_status()
        agents = r.json()
    healthy = [a for a in agents if a.get("healthy")]
    if not healthy:
        print("NO HEALTHY AGENT", agents, file=sys.stderr)
        return 2
    agent = healthy[0]
    print(f"Using agent {agent['agent_name']} @ {agent['url']}; hitl_tools={agent.get('hitl_tools')}")

    runs = [SessionRun(i, USER_PROMPTS[i], agent["url"], agent["agent_name"]) for i in range(10)]

    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    async with httpx.AsyncClient(limits=limits) as client:
        await asyncio.gather(*(r.drive(client, t0) for r in runs))

    wall = time.monotonic() - t0

    # ─── Per-session report ──────────────────────────────────────────────
    print("\n" + "=" * 78)
    print(f"STRESS TEST DONE — wall={wall:.2f}s, sessions={len(runs)}")
    print("=" * 78)
    for r in runs:
        bashes = [c for c in r.confirms_seen if c["tool_name"] == "bash"]
        print(
            f"\n[S{r.idx}] sid={r.session_id}  turns={len(r.prompts)}  "
            f"errors={len(r.errors)}  tool_calls={len(r.tool_calls)}  "
            f"tool_results={len(r.tool_results)}  "
            f"confirms={len(r.confirms_seen)} (bash={len(bashes)})  "
            f"approved={len(r.confirms_approved)}  "
            f"done_reasons={r.done_reasons}  "
            f"turn_ms={[round(x) for x in r.turn_latencies_ms]}"
        )
        print(f"       prompts: {[p[:80] for p in r.prompts]}")
        hist = Counter(e["event"] for e in r.events if not e["event"].startswith("_"))
        print(f"       event_hist: {dict(hist)}")
        text_buf: List[str] = []
        for e in r.events:
            if e["event"] == "text_delta":
                text_buf.append(e["data"].get("text", ""))
            elif e["event"] == "tool_call":
                text_buf.append(f"[TOOL_CALL:{e['data'].get('name')}({json.dumps(e['data'].get('args', {}))[:80]})]")
        full = "".join(text_buf).strip()
        if full:
            print(f"       response: {full[:400]}")
        if r.errors:
            print("       ERRORS:")
            for err in r.errors:
                print("         -", err)

    # ─── Cross-checks ────────────────────────────────────────────────────
    print("\n" + "-" * 78)
    print("CROSS-CHECKS")
    print("-" * 78)
    issues: List[str] = []

    for r in runs:
        call_ids = [c["id"] for c in r.tool_calls]
        result_ids = [c["id"] for c in r.tool_results]
        if Counter(call_ids) != Counter(result_ids):
            issues.append(
                f"[S{r.idx}] tool_call/result id mismatch: calls={call_ids} results={result_ids}"
            )

        for c in r.confirms_seen:
            call_pos = None
            result_pos = None
            confirm_pos = None
            for i, e in enumerate(r.events):
                if e["turn"] != c["turn"]:
                    continue
                if e["event"] == "tool_call" and e["data"].get("name") == c["tool_name"] and call_pos is None:
                    call_pos = i
                if e["event"] == "confirm_request" and e["data"].get("request_id") == c["request_id"]:
                    confirm_pos = i
                if (
                    e["event"] == "tool_result"
                    and e["data"].get("name") == c["tool_name"]
                    and result_pos is None
                    and confirm_pos is not None
                ):
                    result_pos = i
            if confirm_pos is None:
                continue
            if call_pos is None or call_pos > confirm_pos:
                issues.append(
                    f"[S{r.idx}] confirm_request for {c['tool_name']} (req={c['request_id']}) "
                    f"arrived before its tool_call (call_pos={call_pos}, confirm_pos={confirm_pos})"
                )
            if result_pos is not None and result_pos < confirm_pos:
                issues.append(
                    f"[S{r.idx}] tool_result for {c['tool_name']} arrived before its confirm_request"
                )

        for turn_idx in range(len(r.prompts)):
            turn_events = [e for e in r.events if e["turn"] == turn_idx]
            if not turn_events:
                continue
            terminals = [e for e in turn_events if e["event"] in {"done", "error"}]
            if not terminals:
                issues.append(f"[S{r.idx}] turn{turn_idx}: stream ended without done/error")
                continue
            last = turn_events[-1]
            if last["event"] not in {"done", "error"}:
                issues.append(
                    f"[S{r.idx}] turn{turn_idx}: events after done/error: "
                    f"{[e['event'] for e in turn_events[turn_events.index(terminals[0])+1:]]}"
                )

        for dr in r.done_reasons:
            if dr in {"hitl_timeout", "error"}:
                issues.append(f"[S{r.idx}] suspicious done stop_reason: {dr}")

    all_req_ids: Dict[str, List[int]] = defaultdict(list)
    for r in runs:
        for c in r.confirms_seen:
            all_req_ids[c["request_id"]].append(r.idx)
    for req, idxs in all_req_ids.items():
        if len(idxs) > 1:
            issues.append(f"RACE: confirm request_id {req} showed up in multiple sessions: {idxs}")

    for r in runs:
        for a in r.confirms_approved:
            if a.get("status") != 200:
                issues.append(
                    f"[S{r.idx}] approve returned {a.get('status')} for {a.get('tool_name')} "
                    f"(req={a.get('request_id')})"
                )

    # ─── HITL timeline dump ──────────────────────────────────────────────
    print("\n" + "-" * 78)
    print("HITL TIMELINE (only sessions with confirms)")
    print("-" * 78)
    for r in runs:
        if not r.confirms_seen:
            continue
        print(f"\n[S{r.idx}] {len(r.confirms_seen)} confirm(s):")
        for c in r.confirms_seen:
            appr = next(
                (a for a in r.confirms_approved if a["request_id"] == c["request_id"]), None
            )
            call_t = next(
                (
                    e["t_rel"]
                    for e in r.events
                    if e["turn"] == c["turn"]
                    and e["event"] == "tool_call"
                    and e["data"].get("name") == c["tool_name"]
                ),
                None,
            )
            confirm_t = next(
                (
                    e["t_rel"]
                    for e in r.events
                    if e["event"] == "confirm_request"
                    and e["data"].get("request_id") == c["request_id"]
                ),
                None,
            )
            result_t = None
            for e in r.events:
                if (
                    e["event"] == "tool_result"
                    and e["turn"] == c["turn"]
                    and e["data"].get("name") == c["tool_name"]
                    and confirm_t is not None
                    and e["t_rel"] > confirm_t
                ):
                    result_t = e["t_rel"]
                    break
            print(
                f"    tool={c['tool_name']:<10} req_id={c['request_id']}  "
                f"call_t={call_t:.2f}s  confirm_t={confirm_t:.2f}s  "
                f"approve_http={appr['status'] if appr else '?'}  "
                f"approve_latency_ms={appr['latency_ms']:.0f}  "
                f"result_t={'%.2fs' % result_t if result_t else '?'}"
            )

    # ─── Aggregate ───────────────────────────────────────────────────────
    turn_times = [t for r in runs for t in r.turn_latencies_ms]
    if turn_times:
        ts = sorted(turn_times)
        p50 = ts[len(ts) // 2]
        p95 = ts[int(len(ts) * 0.95)]
        print(
            f"\nturn latency (ms): n={len(turn_times)}, min={min(turn_times):.0f}, "
            f"p50={p50:.0f}, p95={p95:.0f}, max={max(turn_times):.0f}"
        )

    total_calls = sum(len(r.tool_calls) for r in runs)
    total_results = sum(len(r.tool_results) for r in runs)
    total_confirms = sum(len(r.confirms_seen) for r in runs)
    total_approves = sum(len(r.confirms_approved) for r in runs)
    bash_confirms = sum(1 for r in runs for c in r.confirms_seen if c["tool_name"] == "bash")
    print(
        f"totals: tool_calls={total_calls} tool_results={total_results} "
        f"confirms={total_confirms} (bash={bash_confirms}) approved={total_approves}"
    )

    print("\nISSUES FOUND:" if issues else "\nNO ISSUES DETECTED")
    for i in issues:
        print("  ✗", i)

    # ─── Cleanup ─────────────────────────────────────────────────────────
    async with httpx.AsyncClient() as c:
        for r in runs:
            if r.session_id:
                try:
                    await c.delete(f"{FRONTEND}/api/sessions/{r.session_id}", timeout=5.0)
                except Exception:
                    pass

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
