// ============================================================
// State
// ============================================================
let sessions = [];
let currentSessionId = null;
let currentStreamController = null;
let markdownBuffer = '';
let renderTimer = null;

// ============================================================
// Marked.js configuration
// ============================================================
marked.setOptions({
    highlight: function(code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    },
    breaks: true,
});

// ============================================================
// Utility
// ============================================================
function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}

function scrollToBottom() {
    const canvas = document.getElementById('chat-canvas');
    canvas.scrollTop = canvas.scrollHeight;
}

// ============================================================
// API
// ============================================================
async function apiLoadSessions() {
    const res = await fetch('/api/sessions');
    return res.json();
}

async function apiCreateSession() {
    const res = await fetch('/api/sessions', { method: 'POST' });
    return res.json();
}

async function apiGetSession(id) {
    const res = await fetch(`/api/sessions/${id}`);
    return res.json();
}

async function apiDeleteSession(id) {
    await fetch(`/api/sessions/${id}`, { method: 'DELETE' });
}

async function apiGetConfig() {
    const res = await fetch('/api/config');
    return res.json();
}

// ============================================================
// Session Sidebar
// ============================================================
function renderSessions() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    for (const s of sessions) {
        const id = s.id || s;
        const isActive = id === currentSessionId;
        const item = document.createElement('div');
        item.className = `chat-item${isActive ? ' active' : ''}`;
        item.innerHTML = `
            <span class="chat-title">${escapeHtml(id.slice(0, 20))}</span>
            <div class="menu-wrapper">
                <button class="options-trigger" onclick="event.stopPropagation(); toggleMenu(this)">
                    <svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1"></circle><circle cx="12" cy="12" r="1"></circle><circle cx="12" cy="19" r="1"></circle></svg>
                </button>
                <div class="dropdown-menu">
                    <div class="menu-item delete" onclick="event.stopPropagation(); doDeleteSession('${id}')">Delete</div>
                </div>
            </div>
        `;
        item.addEventListener('click', () => selectSession(id));
        list.appendChild(item);
    }
}

function toggleMenu(btn) {
    const wrapper = btn.parentElement;
    const wasOpen = wrapper.classList.contains('open');
    document.querySelectorAll('.menu-wrapper.open').forEach(w => w.classList.remove('open'));
    if (!wasOpen) wrapper.classList.add('open');
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.menu-wrapper')) {
        document.querySelectorAll('.menu-wrapper.open').forEach(w => w.classList.remove('open'));
    }
});

async function loadSessions() {
    sessions = await apiLoadSessions();
    renderSessions();
}

async function createNewSession() {
    const data = await apiCreateSession();
    currentSessionId = data.id;
    sessions.unshift({ id: data.id });
    renderSessions();
    document.getElementById('messages-container').innerHTML = '';
}

async function selectSession(id) {
    currentSessionId = id;
    renderSessions();
    const data = await apiGetSession(id);
    renderHistory(data.messages || []);
}

async function doDeleteSession(id) {
    await apiDeleteSession(id);
    sessions = sessions.filter(s => (s.id || s) !== id);
    if (currentSessionId === id) {
        currentSessionId = null;
        document.getElementById('messages-container').innerHTML = '';
    }
    renderSessions();
}

// ============================================================
// Message Rendering (history)
// ============================================================
function renderHistory(messages) {
    const container = document.getElementById('messages-container');
    container.innerHTML = '';
    for (const msg of messages) {
        if (msg.role === 'user') {
            appendUserMessageEl(msg.content);
        } else if (msg.role === 'assistant') {
            const el = createAssistantMessageEl();
            const stream = el.querySelector('.stream-container');
            if (msg.thinking) {
                const thinkEl = document.createElement('div');
                thinkEl.className = 'thinking-block';
                thinkEl.textContent = msg.thinking;
                stream.appendChild(thinkEl);
            }
            if (msg.tool_calls) {
                for (const tc of msg.tool_calls) {
                    addToolCallEl(stream, { id: '', name: tc.name, args_summary: JSON.stringify(tc.args).slice(0, 80) });
                    const blocks = stream.querySelectorAll('.tool-call-block');
                    const last = blocks[blocks.length - 1];
                    if (last) last.querySelector('.tool-status').className = 'tool-status success';
                }
            }
            if (msg.content) {
                const respEl = document.createElement('div');
                respEl.className = 'response-content';
                respEl.innerHTML = marked.parse(msg.content);
                stream.appendChild(respEl);
            }
            if (msg.token_usage) {
                setTokenUsage(stream, msg.token_usage);
            }
        }
    }
    scrollToBottom();
}

// ============================================================
// DOM Element Builders
// ============================================================
function appendUserMessageEl(text) {
    const container = document.getElementById('messages-container');
    const el = document.createElement('div');
    el.className = 'message user-message';
    el.innerHTML = `<div class="message-bubble">${escapeHtml(text)}</div>`;
    container.appendChild(el);
    scrollToBottom();
}

const COLLAPSE_LABEL_EXPANDED  = '▾ thinking process';
const COLLAPSE_LABEL_COLLAPSED = '▸ thinking process collapsed';

function createAssistantMessageEl() {
    const container = document.getElementById('messages-container');
    const el = document.createElement('div');
    el.className = 'message assistant-message';
    el.innerHTML = `
        <div class="collapse-indicator">${COLLAPSE_LABEL_EXPANDED}</div>
        <div class="stream-container"></div>
    `;
    // Per-message collapse toggle (replaces the old global top-bar button).
    el.querySelector('.collapse-indicator').addEventListener('click', () => {
        const collapsed = el.classList.toggle('collapsed');
        el.querySelector('.collapse-indicator').textContent =
            collapsed ? COLLAPSE_LABEL_COLLAPSED : COLLAPSE_LABEL_EXPANDED;
    });
    container.appendChild(el);
    return el;
}

function setAssistantCollapsed(assistantEl, collapsed) {
    if (!assistantEl) return;
    assistantEl.classList.toggle('collapsed', collapsed);
    const ind = assistantEl.querySelector('.collapse-indicator');
    if (ind) ind.textContent = collapsed ? COLLAPSE_LABEL_COLLAPSED : COLLAPSE_LABEL_EXPANDED;
}

function addToolCallEl(container, data) {
    const el = document.createElement('div');
    const isMcp = data.name && data.name.startsWith('mcp_');
    const isAdf = data.name && data.name.startsWith('mcp_adf_');
    el.className = `tool-call-block${isAdf ? ' adf-tool' : ''}${isMcp ? ' mcp' : ''}`;
    el.setAttribute('data-tool-id', data.id || '');

    // Both MCP and non-MCP tools share the same expandable structure.
    // Only difference: MCP expansion includes a pretty-printed input section
    // (since the one-line args_summary isn't always enough).
    let inputSection = '';
    if (isMcp) {
        let inputJson;
        try {
            inputJson = JSON.stringify(data.args || {}, null, 2);
        } catch (e) {
            inputJson = String(data.args);
        }
        inputSection = `
            <div class="tool-details-label">input:</div>
            <pre class="tool-input-pre">${escapeHtml(inputJson)}</pre>
        `;
    }

    el.innerHTML = `
        <div class="tool-header">
            <span class="tool-status running"></span>
            <span class="tool-name">${escapeHtml(data.name || '')}</span>
            <span class="tool-args">${escapeHtml(data.args_summary || '')}</span>
            <span class="toggle-result">show</span>
        </div>
        <div class="tool-details" style="display:none;">
            ${inputSection}
            <div class="tool-details-label">output:</div>
            <pre class="tool-output-pre"></pre>
        </div>
    `;
    // addEventListener (not inline onclick) so the handler doesn't depend
    // on the function being hoisted to the global window scope.
    el.querySelector('.toggle-result').addEventListener('click', (e) => {
        toggleToolDetails(e.currentTarget);
    });
    container.appendChild(el);
}

function toggleToolDetails(trigger) {
    const block = trigger.closest('.tool-call-block');
    const details = block.querySelector('.tool-details');
    if (!details) return;
    if (details.style.display === 'none') {
        details.style.display = 'block';
        trigger.textContent = 'hide';
    } else {
        details.style.display = 'none';
        trigger.textContent = 'show';
    }
}

function updateToolResultEl(container, data) {
    const blocks = container.querySelectorAll('.tool-call-block');
    let target = null;
    for (const block of blocks) {
        if (block.getAttribute('data-tool-id') === data.id) {
            target = block;
            break;
        }
    }
    if (!target) return;

    target.querySelector('.tool-status').className = `tool-status ${data.is_error ? 'error' : 'success'}`;
    target.classList.add('has-result');

    const out = target.querySelector('.tool-output-pre');
    if (out) {
        out.textContent = data.output || '';
        if (data.is_error) out.classList.add('error');
    }
}

function formatUsageRow(label, usage) {
    // usage = { input, output, cache_creation, cache_read, cost }
    // Anthropic API splits input into three disjoint buckets:
    //   - input          = uncached, newly-read input
    //   - cache_read     = input served from cache
    //   - cache_creation = input newly written into cache
    // Match the CLI convention (tracking.format_turn): display `in` as the
    // combined total of all three, then show cache_read / cache_write as
    // sub-metrics so the user can see how much of `in` came from cache.
    const n = (v) => (v || 0).toLocaleString();
    const combinedIn = (usage.input || 0) + (usage.cache_read || 0) + (usage.cache_creation || 0);
    const parts = [label, `in:${combinedIn.toLocaleString()}`, `out:${n(usage.output)}`];
    if (usage.cache_read)     parts.push(`cache_read:${n(usage.cache_read)}`);
    if (usage.cache_creation) parts.push(`cache_write:${n(usage.cache_creation)}`);
    if (typeof usage.cost === 'number') parts.push(`$${usage.cost.toFixed(4)}`);
    return parts.join(' ');
}

function setTokenUsage(container, data) {
    if (!container) return;

    const turn  = data.turn  || {};
    const total = data.total || {};

    const wrap = document.createElement('div');
    wrap.className = 'token-usage-line';

    const turnRow = document.createElement('div');
    turnRow.textContent = formatUsageRow('turn ', turn);

    const totalRow = document.createElement('div');
    totalRow.textContent = formatUsageRow('total', total);

    wrap.appendChild(turnRow);
    wrap.appendChild(totalRow);
    container.appendChild(wrap);
}

function finalizeAssistantMessage(assistantEl) {
    assistantEl.querySelectorAll('.response-content.streaming-cursor')
        .forEach(el => el.classList.remove('streaming-cursor'));
    assistantEl.querySelectorAll('.thinking-block.streaming')
        .forEach(el => el.classList.remove('streaming'));
}

// ============================================================
// SSE Streaming
// ============================================================
function parseSSE(buffer) {
    // SSE events are terminated by a blank line ("\n\n"). Only process
    // fully-received events; keep any trailing partial event as `remaining`
    // so it can be concatenated with the next network chunk. The previous
    // line-by-line parser couldn't distinguish a partial line at the end of
    // a chunk from a complete line, which silently dropped large events
    // (e.g. todo_write with long Chinese payloads) when they straddled a
    // chunk boundary.
    const events = [];
    // sse_starlette (and the SSE spec) uses CRLF; older proxies/servers may
    // use LF. Split on either so we don't accumulate everything in `remaining`.
    const parts = buffer.split(/\r?\n\r?\n/);
    const remaining = parts.pop() ?? '';

    for (const raw of parts) {
        if (!raw) continue;
        let eventType = 'message';
        let dataStr = '';
        for (const line of raw.split(/\r?\n/)) {
            if (line.startsWith('event: ')) {
                eventType = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
                dataStr += (dataStr ? '\n' : '') + line.slice(6);
            }
        }
        try {
            events.push({ type: eventType, data: JSON.parse(dataStr) });
        } catch (e) {}
    }
    return { parsed: events, remaining };
}

async function sendMessage(sessionId, message) {
    appendUserMessageEl(message);

    const assistantEl = createAssistantMessageEl();
    const state = {
        assistantEl,
        stream: assistantEl.querySelector('.stream-container'),
        currentThinking: null,
        currentText: null,
    };

    markdownBuffer = '';
    currentStreamController = new AbortController();
    disableInput();

    try {
        const response = await fetch(`/api/sessions/${sessionId}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message }),
            signal: currentStreamController.signal,
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const events = parseSSE(buffer);
            buffer = events.remaining;

            for (const event of events.parsed) {
                handleStreamEvent(event, state);
            }
        }
    } catch (e) {
        if (e.name !== 'AbortError') {
            const errEl = document.createElement('div');
            errEl.style.color = '#ef4444';
            errEl.textContent = `Error: ${e.message}`;
            state.stream.appendChild(errEl);
        }
    } finally {
        enableInput();
        currentStreamController = null;
        if (state.currentText) flushMarkdown(state.currentText);
        finalizeAssistantMessage(assistantEl);
        // Auto-collapse THIS message once its turn is done so focus returns
        // to the assistant's prose. User can click the indicator to re-expand.
        setAssistantCollapsed(assistantEl, true);
        scrollToBottom();
    }
}

function closeCurrentText(state) {
    if (state.currentText) {
        flushMarkdown(state.currentText);
        state.currentText.classList.remove('streaming-cursor');
        state.currentText = null;
        markdownBuffer = '';
    }
}

function closeCurrentThinking(state) {
    if (state.currentThinking) {
        state.currentThinking.classList.remove('streaming');
        state.currentThinking = null;
    }
}

function handleStreamEvent(event, state) {
    switch (event.type) {
        case 'thinking_start': {
            closeCurrentText(state);
            closeCurrentThinking(state);
            const el = document.createElement('div');
            el.className = 'thinking-block streaming';
            state.stream.appendChild(el);
            state.currentThinking = el;
            scrollToBottom();
            break;
        }

        case 'thinking_delta':
            if (state.currentThinking) {
                state.currentThinking.textContent += event.data.text;
                scrollToBottom();
            }
            break;

        case 'thinking_stop':
            closeCurrentThinking(state);
            break;

        case 'text_delta':
            if (!state.currentText) {
                const el = document.createElement('div');
                el.className = 'response-content streaming-cursor';
                state.stream.appendChild(el);
                state.currentText = el;
                markdownBuffer = '';
            }
            markdownBuffer += event.data.text;
            debouncedRenderMarkdown(state.currentText);
            scrollToBottom();
            break;

        case 'text_stop':
            closeCurrentText(state);
            break;

        case 'tool_call':
            closeCurrentText(state);
            closeCurrentThinking(state);
            addToolCallEl(state.stream, event.data);
            scrollToBottom();
            break;

        case 'tool_result':
            updateToolResultEl(state.stream, event.data);
            break;

        case 'token_usage':
            closeCurrentText(state);
            setTokenUsage(state.stream, event.data);
            break;

        case 'confirm_request':
            handleConfirmRequest(event.data, state.stream);
            break;

        case 'done':
            break;

        case 'error': {
            const errEl = document.createElement('div');
            errEl.style.color = '#ef4444';
            errEl.textContent = `Error: ${event.data.message}`;
            state.stream.appendChild(errEl);
            break;
        }
    }
}

function handleConfirmRequest(data, toolsContainer) {
    const el = document.createElement('div');
    el.className = 'confirm-block';
    const preview = data.preview ? ` ${data.preview}` : '';
    el.innerHTML = `
        <div class="confirm-prompt">
            <span class="confirm-icon">?</span>
            <span>Allow <strong>${escapeHtml(data.tool_name)}</strong>${escapeHtml(preview)}?</span>
        </div>
        <div class="confirm-actions">
            <button class="confirm-btn allow" onclick="respondConfirm(this, true)">Allow</button>
            <button class="confirm-btn deny" onclick="respondConfirm(this, false)">Deny</button>
        </div>
    `;
    toolsContainer.appendChild(el);
    scrollToBottom();
}

async function respondConfirm(btn, allowed) {
    const block = btn.closest('.confirm-block');
    if (allowed) {
        // Allow: remove the confirm block entirely — no need to keep visual
        // clutter for an action the user approved.
        block.remove();
    } else {
        // Deny: keep the block in place, disabled, marked "Denied" so the
        // user can see which tool was rejected.
        block.querySelectorAll('.confirm-btn').forEach(b => b.disabled = true);
        block.querySelector('.confirm-prompt').style.opacity = '0.6';
        block.querySelector('.confirm-actions').innerHTML =
            `<span style="color:#ef4444;font-size:0.8rem;">Denied</span>`;
    }
    await fetch('/api/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allowed }),
    });
}

// Debounced markdown rendering (50ms)
function debouncedRenderMarkdown(el) {
    if (renderTimer) return;
    renderTimer = setTimeout(() => {
        renderTimer = null;
        flushMarkdown(el);
    }, 50);
}

function flushMarkdown(el) {
    if (renderTimer) {
        clearTimeout(renderTimer);
        renderTimer = null;
    }
    if (markdownBuffer) {
        el.innerHTML = marked.parse(markdownBuffer);
    }
}

// ============================================================
// Input Management
// ============================================================
function disableInput() {
    document.getElementById('input-text').disabled = true;
    document.getElementById('send-btn').disabled = true;
}

function enableInput() {
    document.getElementById('input-text').disabled = false;
    document.getElementById('send-btn').disabled = false;
    document.getElementById('input-text').focus();
}

// Auto-grow textarea
const inputText = document.getElementById('input-text');
inputText.addEventListener('input', () => {
    inputText.style.height = 'auto';
    inputText.style.height = Math.min(inputText.scrollHeight, 200) + 'px';
});

// Enter to send, Shift+Enter for newline
inputText.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        doSend();
    }
});

document.getElementById('send-btn').addEventListener('click', doSend);

async function doSend() {
    const input = document.getElementById('input-text');
    const msg = input.value.trim();
    if (!msg) return;
    if (!currentSessionId) {
        await createNewSession();
    }
    input.value = '';
    input.style.height = 'auto';
    await sendMessage(currentSessionId, msg);
}

// ============================================================
// New Chat Button
// ============================================================
document.getElementById('new-chat-btn').addEventListener('click', createNewSession);

// ============================================================
// Init
// ============================================================
async function init() {
    const config = await apiGetConfig();
    document.getElementById('model-display').textContent = `Agent (${config.model || 'unknown'})`;
    document.getElementById('model-tag').textContent = config.model || '';

    await loadSessions();

    // Auto-create a session if none exist
    if (sessions.length === 0) {
        await createNewSession();
    } else {
        const firstId = sessions[0].id || sessions[0];
        await selectSession(firstId);
    }
}

init();
