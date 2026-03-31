# Context Compression 上下文压缩

## 概述

Agent 在长对话中会不断积累 tool result、thinking block 等内容，很快就会撑满 context window。压缩模块通过三层策略逐步释放空间，在不丢失关键信息的前提下实现无限会话。

相关文件：
- `agent_runtime/compression.py` — 压缩逻辑
- `agent_runtime/loop.py` — 主循环中的压缩集成
- `agent_runtime/tracking.py` — token 用量追踪（为压缩决策提供精确数据）
- `agent_runtime/config.py` — `COMPACT_THRESHOLD`、`KEEP_RECENT` 等配置

## 三层压缩架构

```
每一轮 agent loop:

[Layer 1: micro_compact]          每轮静默执行
  ├─ 剥离旧 thinking block
  └─ 替换旧 tool_result 为占位符
          │
          v
[Check: should_compact(tracker)?]  用上一轮 API 返回的真实 token 数判断
   │                │
   no               yes
   │                │
   v                v
 继续        [Layer 2: auto_compact]
               ├─ 保存完整 transcript 到磁盘
               ├─ LLM 生成摘要
               └─ 替换所有消息为 [summary + assistant ack]
                        │
                        v
               [Layer 3: compact tool]
                 模型主动调用 compact 工具
                 触发同样的 auto_compact 流程
```

## Layer 1: micro_compact

**触发时机**：每轮 LLM 调用前自动执行。

**做两件事**：

### 1. 剥离旧 thinking block

当 `THINKING_ENABLED=True` 时，Anthropic API 返回的 assistant 消息会包含 `ThinkingBlock`。但旧轮次的 thinking block 对 API 没有意义（API 只看最新一轮的 thinking），留着只是白占 token。

```python
# 除了最后一条消息外，移除所有 assistant 消息中的 thinking block
for msg in messages[:-1]:
    if msg["role"] == "assistant" and isinstance(msg.get("content"), list):
        msg["content"] = [
            b for b in msg["content"]
            if not (hasattr(b, "type") and b.type == "thinking")
        ]
```

**为什么是移除而不是替换**：直接从列表中过滤掉，不需要 mutate SDK 对象（SDK 对象可能是 frozen 的），也不会留下占 token 的占位符字符串。

### 2. 替换旧 tool_result

保留最近 `KEEP_RECENT`（默认 3）个 tool_result 不动，更早的 tool_result 如果内容超过 100 字符，替换为 `[Previous: used {tool_name}]`。

```python
# 工作流程：
# 1. 收集所有 tool_result
# 2. 构建 tool_use_id → tool_name 的映射（从 assistant 消息的 ToolUseBlock 中提取）
# 3. 替换 KEEP_RECENT 之前的旧结果
```

tool_name 映射是必要的，因为 tool_result 本身只有 `tool_use_id`，没有工具名。占位符里保留工具名让模型知道之前用过什么工具，只是具体输出被压缩了。

## Layer 2: auto_compact

**触发时机**：`should_compact(tracker)` 返回 True 时自动触发。

### 触发判断：should_compact

```python
def should_compact(tracker) -> bool:
    if not tracker or not tracker._turns:
        return False
    last = tracker._turns[-1]
    actual_context = (
        last.input_tokens
        + last.cache_read_input_tokens
        + last.cache_creation_input_tokens
    )
    return actual_context > config.COMPACT_THRESHOLD
```

**关键设计决策**：使用上一轮 API 返回的**真实 token 数**，而不是启发式估算。

之前的实现用 `len(str(messages)) // 4`，这个方法有严重缺陷：
- `str()` 输出的是 Python repr，包含大量结构性字符（`'role':`, `TextBlock(`, `type=` 等），这些不存在于实际 token 流中
- `÷4` 的比例只对英文散文大致成立，中文、代码、JSON 的 token/char 比例完全不同
- 不包含 system prompt 和 tool definitions 的 token 开销（这部分可能有数千 token）

而 `TokenTracker` 每轮都从 API response 中记录了精确的 usage：
- `input_tokens`：未命中缓存的输入 token
- `cache_read_input_tokens`：从缓存读取的 token
- `cache_creation_input_tokens`：写入缓存的 token

三者之和 = **发给 API 的完整 context 大小**（包含 system + tools + messages），这正是我们需要的精确度量。

第一轮没有历史 usage 数据时返回 False——第一轮 context 本来就小，不需要压缩。

### auto_compact 流程

1. **保存 transcript**：完整对话写入 `.transcripts/transcript_{timestamp}.jsonl`，信息不丢失，只是移出活跃 context。transcript 路径打印到 stderr 供开发者 debug，不暴露给模型。

2. **LLM 生成摘要**：取对话 JSON 的**尾部** 80000 字符（最近的对话），让 LLM 提取：已完成的工作、当前状态、关键决策。取尾部而非头部，因为触发压缩时对话可能有 200K+ 字符，截头部会丢失最近的上下文——恰恰是最关键的信息。摘要 API call 的 token 消耗会计入 `TokenTracker`，确保用户看到的 cost 是准确的。

   > **Cost 优化提示**：当前摘要使用 `config.MODEL`（主模型）。如果主模型是 Opus，每次压缩的摘要 cost 会比较高。摘要是个相对简单的任务，可以考虑用 Haiku 替代（`claude-haiku-4-5-20251001`），能大幅降低压缩成本。如需切换，修改 `auto_compact` 中的 `model=config.MODEL` 为目标模型 ID 即可。

3. **返回压缩后的消息列表**：

```python
return [
    {"role": "user", "content": f"[Conversation compressed]\n\n{summary}"},
    {"role": "assistant", "content": "Understood. Continuing from the summary above."},
]
```

**必须包含 assistant 消息**的原因：
- Anthropic API 要求消息严格 user/assistant 交替
- 压缩后 `_inject_todo` 会把 todo 内容合并进 `messages[0]`（user 消息）
- 如果没有 assistant 消息，下一轮 loop 添加 tool result（user 角色）时会出现连续两条 user 消息，导致 API 400 错误

**不在 user 消息中包含指令**（如 "Continue working."）的原因：
- user 消息放纯上下文（summary），assistant 消息表示确认
- 职责分离让模型更清楚哪些是历史上下文、哪些是行动指令

**不暴露 transcript 路径给模型**的原因：
- transcript 是开发者 debug 用的，模型拿到路径可能尝试 `read_file` 读取
- transcript 文件可能几万行，读进来会重新撑爆 context，形成 compact → 读 transcript → 又满 → compact 的死循环

## Layer 3: compact tool（手动触发）

模型可以主动调用 `compact` 工具。在 `loop.py` 中检测到 `block.name == "compact"` 时，执行与 auto_compact 完全相同的流程。适用于模型判断当前对话已经太长需要整理的场景。

## _inject_todo：压缩后的 todo 恢复

todo 状态需要在压缩后保留（否则模型会忘记任务计划）。`_inject_todo` 在 auto_compact 之后把当前 todo 内容注入回消息列表。

```python
def _inject_todo(messages: list):
    if not (tools_mod.TODO and tools_mod.TODO.has_content):
        return
    todo_block = f"<todo>\n{tools_mod.TODO.read()}\n</todo>\n\n"
    # 合并进 messages[0]（user 消息），而不是 insert(0) 创建新消息
    if messages and messages[0]["role"] == "user" and isinstance(messages[0]["content"], str):
        messages[0]["content"] = todo_block + messages[0]["content"]
```

**合并而非插入**的原因：auto_compact 返回的 `messages[0]` 一定是 user 消息。如果用 `insert(0, user_msg)` 会在 user 消息前面再插一条 user 消息，破坏 user/assistant 交替，导致 API 报错。

## 配置项

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `COMPACT_THRESHOLD` | 50000 | 触发 auto_compact 的 token 阈值（基于 API 返回的真实 input token 数） |
| `KEEP_RECENT` | 3 | micro_compact 保留最近几个 tool_result 不压缩 |

## 数据流全景

```
agent_loop 每一轮:

  ┌─────────────────────────────────────────────────────────┐
  │ micro_compact(messages)                                 │
  │   - 剥离旧 thinking block                               │
  │   - 替换旧 tool_result → "[Previous: used X]"           │
  └─────────────────────────────────────────────────────────┘
                          │
                          v
  ┌─────────────────────────────────────────────────────────┐
  │ should_compact(tracker)?                                │
  │   - 取上一轮 API response 的 input_tokens               │
  │   - 加上 cache_read + cache_creation = 真实 context 大小 │
  │   - > COMPACT_THRESHOLD? → auto_compact                 │
  └─────────────────────────────────────────────────────────┘
                          │
              ┌───── yes ─┴─ no ─────┐
              v                      v
  ┌───────────────────────────────┐   ┌──────────────────┐
  │ auto_compact(messages,tracker) │   │ 跳过，继续       │
  │  1. 保存 transcript            │   └──────────────────┘
  │  2. LLM 做摘要（取尾部 80K）   │
  │  3. tracker.record(usage)      │
  │  4. 返回 [user, asst]          │
  │  5. _inject_todo 合并           │
  └───────────────────────────────┘
                          │
                          v
  ┌─────────────────────────────────────────────────────────┐
  │ _stream_response(system, messages)                      │
  │   → API 调用，返回 content_blocks + usage               │
  └─────────────────────────────────────────────────────────┘
                          │
                          v
  ┌─────────────────────────────────────────────────────────┐
  │ tracker.record(usage)                                   │
  │   → 记录真实 token 数，下一轮 should_compact 用          │
  └─────────────────────────────────────────────────────────┘
                          │
                          v
                    处理 tool_use...
                    （如果模型调了 compact → 手动触发 auto_compact）
```
