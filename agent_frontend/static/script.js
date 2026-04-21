// ============================================================
// Agent Frontend — single-page chat UI for agent-runtime.
//
// State model (simple, single-user):
//   sessions         — metadata list for the sidebar (id, title, agent, ts)
//   currentSessionId — the active chat
//   currentSession   — full session incl. Anthropic-shape messages[]
//   agents           — probed agent-runtimes (for the top-bar picker)
//   selectedAgent    — agent used when creating the next chat
//
// The runtime is stateless: every /api/chat POST carries the full
// messages[]. We accumulate assistant turns from SSE events locally, then
// PUT the session back to the server when the stream ends.
// ============================================================

let sessions = [];
let currentSessionId = null;
let currentSession = null;
let agents = [];
let selectedAgent = null;

let currentStreamController = null;
let pendingConfirm = null;
let markdownBuffer = '';
let renderTimer = null;

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

function titleFromFirstMessage(text, max = 30) {
    const cleaned = (text || '').trim().replace(/\s+/g, ' ');
    if (!cleaned) return 'New chat';
    return cleaned.length <= max ? cleaned : cleaned.slice(0, max) + '…';
}

function uuidLike() {
    return 'xxxxxxxxxxxxxxxx'.replace(/x/g,
        () => Math.floor(Math.random() * 16).toString(16));
}

// ============================================================
// API
// ============================================================
async function apiListSessions() {
    const r = await fetch('/api/sessions');
    return r.json();
}

async function apiCreateSession(agent) {
    const r = await fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            agent_url:  agent?.url  || '',
            agent_name: agent?.agent_name || '',
        }),
    });
    return r.json();
}

async function apiGetSession(id) {
    const r = await fetch(`/api/sessions/${id}`);
    if (!r.ok) return null;
    return r.json();
}

async function apiPutSession(session) {
    const r = await fetch(`/api/sessions/${session.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(session),
    });
    return r.json();
}

async function apiDeleteSession(id) {
    await fetch(`/api/sessions/${id}`, { method: 'DELETE' });
}

async function apiListAgents() {
    const r = await fetch('/api/agents');
    return r.json();
}

// ============================================================
// Sidebar
// ============================================================
function renderSessions() {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    for (const s of sessions) {
        const isActive = s.id === currentSessionId;
        const item = document.createElement('div');
        item.className = `chat-item${isActive ? ' active' : ''}`;
        item.innerHTML = `
            <span class="chat-title">${escapeHtml(s.title || 'New chat')}</span>
            <div class="menu-wrapper">
                <button class="options-trigger" onclick="event.stopPropagation(); toggleMenu(this)">
                    <svg class="icon icon-sm" viewBox="0 0 24 24">
                        <circle cx="12" cy="5" r="1"></circle>
                        <circle cx="12" cy="12" r="1"></circle>
                        <circle cx="12" cy="19" r="1"></circle>
                    </svg>
                </button>
                <div class="dropdown-menu">
                    <div class="menu-item delete" onclick="event.stopPropagation(); doDeleteSession('${s.id}')">Delete</div>
                </div>
            </div>
        `;
        item.addEventListener('click', () => selectSession(s.id));
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
    sessions = await apiListSessions();
    renderSessions();
}

async function createNewSession() {
    if (!selectedAgent || !selectedAgent.healthy) {
        alert('No healthy agent runtime. Check AGENT_RUNTIMES.');
        return;
    }
    const s = await apiCreateSession(selectedAgent);
    sessions.unshift(s);
    currentSessionId = s.id;
    currentSession = s;
    document.getElementById('messages-container').innerHTML = '';
    renderSessions();
}

async function selectSession(id) {
    currentSessionId = id;
    currentSession = await apiGetSession(id);
    renderSessions();
    renderHistory(currentSession?.messages || []);
}

async function doDeleteSession(id) {
    await apiDeleteSession(id);
    sessions = sessions.filter(s => s.id !== id);
    if (currentSessionId === id) {
        currentSessionId = null;
        currentSession = null;
        document.getElementById('messages-container').innerHTML = '';
    }
    renderSessions();
}

// ============================================================
// Agent picker
// ============================================================
function renderAgentPicker() {
    const sel = document.getElementById('agent-picker');
    sel.innerHTML = '';
    for (const a of agents) {
        const opt = document.createElement('option');
        opt.value = a.url;
        opt.textContent = a.healthy
            ? `${a.agent_name || 'agent'} · ${a.model || ''}`
            : `${a.url} (offline)`;
        opt.disabled = !a.healthy;
        sel.appendChild(opt);
    }
    if (selectedAgent) sel.value = selectedAgent.url;
    sel.onchange = () => {
        selectedAgent = agents.find(a => a.url === sel.value) || null;
    };
}

// ============================================================
// History rendering
// ============================================================
function renderHistory(messages) {
    const container = document.getElementById('messages-container');
    container.innerHTML = '';
    for (let i = 0; i < messages.length; i++) {
        const msg = messages[i];
        if (msg.role === 'user') {
            if (typeof msg.content === 'string') appendUserMessageEl(msg.content);
            // tool_result user messages merge into their assistant's tool cards
        } else if (msg.role === 'assistant') {
            renderAssistantHistory(msg, messages, i);
        }
    }
    scrollToBottom();
}

function renderAssistantHistory(msg, all, myIdx) {
    const el = createAssistantMessageEl();
    const stream = el.querySelector('.stream-container');
    const blocks = Array.isArray(msg.content) ? msg.content : [];

    // Find tool_results in user messages between this assistant and the next
    // role boundary, so we can fill in each tool card's output.
    const resultsById = {};
    for (let i = myIdx + 1; i < all.length; i++) {
        const m = all[i];
        if (m.role === 'user' && Array.isArray(m.content)) {
            for (const b of m.content) {
                if (b.type === 'tool_result') resultsById[b.tool_use_id] = b;
            }
        } else {
            break;
        }
    }

    for (const block of blocks) {
        if (block.type === 'text') {
            const respEl = document.createElement('div');
            respEl.className = 'response-content';
            respEl.innerHTML = marked.parse(block.text || '');
            stream.appendChild(respEl);
        } else if (block.type === 'thinking') {
            const thinkEl = document.createElement('div');
            thinkEl.className = 'thinking-block';
            thinkEl.textContent = block.thinking || '';
            stream.appendChild(thinkEl);
        } else if (block.type === 'tool_use') {
            addToolCallEl(stream, {
                id: block.id,
                name: block.name,
                args: block.input,
                args_summary: block.args_summary || '',
            });
            const result = resultsById[block.id];
            if (result) {
                updateToolResultEl(stream, {
                    id: block.id,
                    output: typeof result.content === 'string'
                        ? result.content : JSON.stringify(result.content),
                    is_error: !!result.is_error,
                });
            }
        }
    }
    if (msg.meta?.tokens) setTokenUsage(stream, msg.meta.tokens);
    setAssistantCollapsed(el, true);
}

// ============================================================
// DOM builders
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
    el.querySelector('.collapse-indicator').addEventListener('click', () => {
        const collapsed = el.classList.toggle('collapsed');
        el.querySelector('.collapse-indicator').textContent =
            collapsed ? COLLAPSE_LABEL_COLLAPSED : COLLAPSE_LABEL_EXPANDED;
    });
    container.appendChild(el);
    return el;
}

function setAssistantCollapsed(el, collapsed) {
    if (!el) return;
    el.classList.toggle('collapsed', collapsed);
    const ind = el.querySelector('.collapse-indicator');
    if (ind) ind.textContent = collapsed ? COLLAPSE_LABEL_COLLAPSED : COLLAPSE_LABEL_EXPANDED;
}

function addToolCallEl(container, data) {
    const el = document.createElement('div');
    const isMcp = data.name && data.name.startsWith('mcp_');
    const isAdf = data.name && data.name.startsWith('mcp_adf_');
    el.className = `tool-call-block${isAdf ? ' adf-tool' : ''}${isMcp ? ' mcp' : ''}`;
    el.setAttribute('data-tool-id', data.id || '');

    let inputSection = '';
    if (isMcp) {
        let inputJson;
        try { inputJson = JSON.stringify(data.args || {}, null, 2); }
        catch (e) { inputJson = String(data.args); }
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
        if (block.getAttribute('data-tool-id') === data.id) { target = block; break; }
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
    const wrap = document.createElement('div');
    wrap.className = 'token-usage-line';
    const t1 = document.createElement('div'); t1.textContent = formatUsageRow('turn ', data.turn  || {});
    const t2 = document.createElement('div'); t2.textContent = formatUsageRow('total', data.total || {});
    wrap.appendChild(t1); wrap.appendChild(t2);
    container.appendChild(wrap);
}

function finalizeAssistantMessage(assistantEl) {
    assistantEl.querySelectorAll('.response-content.streaming-cursor')
        .forEach(el => el.classList.remove('streaming-cursor'));
    assistantEl.querySelectorAll('.thinking-block.streaming')
        .forEach(el => el.classList.remove('streaming'));
}

// ============================================================
// SSE parsing (CRLF + LF safe)
// ============================================================
function parseSSE(buffer) {
    const events = [];
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
        try { events.push({ type: eventType, data: JSON.parse(dataStr) }); }
        catch (e) {}
    }
    return { parsed: events, remaining };
}

// ============================================================
// Streaming state machine
//
// Multi-turn within one /api/chat round: commit the current assistant
// turn + its tool_results on the tool_result→new-LLM-event boundary, so
// the stored history stays alternating-role (Anthropic requirement).
// ============================================================
async function sendMessage(message) {
    if (!currentSession) return;

    currentSession.messages.push({ role: 'user', content: message });
    if (currentSession.title === 'New chat') {
        currentSession.title = titleFromFirstMessage(message);
        const idx = sessions.findIndex(s => s.id === currentSession.id);
        if (idx >= 0) sessions[idx] = { ...sessions[idx], title: currentSession.title };
        renderSessions();
    }
    await apiPutSession(currentSession);

    appendUserMessageEl(message);
    const assistantEl = createAssistantMessageEl();
    const state = {
        assistantEl,
        stream: assistantEl.querySelector('.stream-container'),
        currentText: null,
        currentThinking: null,
        cur: { role: 'assistant', content: [], meta: {} },
        pendingResults: [],
    };
    markdownBuffer = '';
    disableInput();
    currentStreamController = new AbortController();

    try {
        const resp = await fetch(`/api/sessions/${currentSession.id}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                messages: currentSession.messages,
                trace_id: uuidLike(),
            }),
            signal: currentStreamController.signal,
        });
        if (!resp.ok) throw new Error(`chat ${resp.status}: ${await resp.text()}`);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const { parsed, remaining } = parseSSE(buffer);
            buffer = remaining;
            for (const ev of parsed) handleStreamEvent(ev, state);
        }
    } catch (e) {
        if (e.name !== 'AbortError') {
            const errEl = document.createElement('div');
            errEl.style.color = '#ef4444';
            errEl.textContent = `Error: ${e.message}`;
            state.stream.appendChild(errEl);
        }
    } finally {
        commitTurn(state);
        try { await apiPutSession(currentSession); }
        catch (e) { console.error('persist failed:', e); }
        enableInput();
        currentStreamController = null;
        finalizeAssistantMessage(state.assistantEl);
        setAssistantCollapsed(state.assistantEl, true);
        scrollToBottom();
    }
}

function commitTurn(state) {
    closeCurrentText(state);
    closeCurrentThinking(state);
    if (state.cur.content.length > 0 || state.cur.meta.tokens) {
        currentSession.messages.push(state.cur);
    }
    if (state.pendingResults.length > 0) {
        currentSession.messages.push({ role: 'user', content: state.pendingResults });
    }
    state.cur = { role: 'assistant', content: [], meta: {} };
    state.pendingResults = [];
}

function maybeBoundary(state) {
    if (state.pendingResults.length > 0) commitTurn(state);
}

function handleStreamEvent(event, state) {
    switch (event.type) {
        case 'thinking_start': {
            maybeBoundary(state);
            closeCurrentText(state);
            closeCurrentThinking(state);
            const el = document.createElement('div');
            el.className = 'thinking-block streaming';
            state.stream.appendChild(el);
            state.currentThinking = el;
            state.cur.content.push({ type: 'thinking', thinking: '' });
            scrollToBottom();
            break;
        }
        case 'thinking_delta':
            if (state.currentThinking) {
                state.currentThinking.textContent += event.data.text;
                const last = lastBlockOfType(state.cur, 'thinking');
                if (last) last.thinking += event.data.text;
                scrollToBottom();
            }
            break;
        case 'thinking_stop':
            closeCurrentThinking(state);
            break;

        case 'text_delta':
            maybeBoundary(state);
            if (!state.currentText) {
                const el = document.createElement('div');
                el.className = 'response-content streaming-cursor';
                state.stream.appendChild(el);
                state.currentText = el;
                state.cur.content.push({ type: 'text', text: '' });
                markdownBuffer = '';
            }
            markdownBuffer += event.data.text;
            {
                const last = lastBlockOfType(state.cur, 'text');
                if (last) last.text = markdownBuffer;
            }
            debouncedRenderMarkdown(state.currentText);
            scrollToBottom();
            break;
        case 'text_stop':
            closeCurrentText(state);
            break;

        case 'tool_call':
            maybeBoundary(state);
            closeCurrentText(state);
            closeCurrentThinking(state);
            addToolCallEl(state.stream, event.data);
            state.cur.content.push({
                type: 'tool_use',
                id: event.data.id,
                name: event.data.name,
                input: event.data.args,
                args_summary: event.data.args_summary || '',
            });
            scrollToBottom();
            break;

        case 'tool_result':
            updateToolResultEl(state.stream, event.data);
            state.pendingResults.push({
                type: 'tool_result',
                tool_use_id: event.data.id,
                content: event.data.output || '',
                is_error: !!event.data.is_error,
            });
            break;

        case 'token_usage':
            closeCurrentText(state);
            setTokenUsage(state.stream, event.data);
            state.cur.meta.tokens = event.data;
            break;

        case 'confirm_request':
            handleConfirmRequest(event.data, state.stream);
            break;

        case 'done':
            if (event.data?.stop_reason && event.data.stop_reason !== 'end_turn') {
                const note = document.createElement('div');
                note.className = 'done-note';
                note.textContent = `(round ended: ${event.data.stop_reason})`;
                state.stream.appendChild(note);
                state.cur.meta.stop_reason = event.data.stop_reason;
            }
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

function lastBlockOfType(assistant, type) {
    for (let i = assistant.content.length - 1; i >= 0; i--) {
        if (assistant.content[i].type === type) return assistant.content[i];
    }
    return null;
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

// ============================================================
// HITL confirm
// ============================================================
function handleConfirmRequest(data, toolsContainer) {
    pendingConfirm = { requestId: data.request_id };
    const el = document.createElement('div');
    el.className = 'confirm-block';
    const preview = data.preview ? ` ${data.preview}` : '';
    el.innerHTML = `
        <div class="confirm-prompt">
            <span class="confirm-icon">?</span>
            <span>Allow <strong>${escapeHtml(data.tool_name)}</strong>${escapeHtml(preview)}?</span>
        </div>
        <div class="confirm-actions">
            <button class="confirm-btn allow">Allow</button>
            <button class="confirm-btn deny">Deny</button>
        </div>
    `;
    el.querySelector('.allow').addEventListener('click', () => respondConfirm(el, data.request_id, true));
    el.querySelector('.deny').addEventListener('click',  () => respondConfirm(el, data.request_id, false));
    toolsContainer.appendChild(el);
    scrollToBottom();
}

async function respondConfirm(block, requestId, allowed) {
    if (allowed) {
        block.remove();
    } else {
        block.querySelectorAll('.confirm-btn').forEach(b => b.disabled = true);
        block.querySelector('.confirm-prompt').style.opacity = '0.6';
        block.querySelector('.confirm-actions').innerHTML =
            `<span style="color:#ef4444;font-size:0.8rem;">Denied</span>`;
    }
    await fetch(`/api/sessions/${currentSessionId}/confirm/${requestId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allowed }),
    });
}

// ============================================================
// Markdown
// ============================================================
function debouncedRenderMarkdown(el) {
    if (renderTimer) return;
    renderTimer = setTimeout(() => { renderTimer = null; flushMarkdown(el); }, 50);
}

function flushMarkdown(el) {
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    if (markdownBuffer) el.innerHTML = marked.parse(markdownBuffer);
}

// ============================================================
// Input
// ============================================================
function disableInput() {
    document.getElementById('input-text').disabled = true;
    document.getElementById('send-btn').disabled   = true;
}

function enableInput() {
    const input = document.getElementById('input-text');
    input.disabled = false;
    document.getElementById('send-btn').disabled = false;
    input.focus();
}

const inputText = document.getElementById('input-text');
inputText.addEventListener('input', () => {
    inputText.style.height = 'auto';
    inputText.style.height = Math.min(inputText.scrollHeight, 200) + 'px';
});
inputText.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        doSend();
    }
});
document.getElementById('send-btn').addEventListener('click', doSend);
document.getElementById('new-chat-btn').addEventListener('click', createNewSession);

async function doSend() {
    const input = document.getElementById('input-text');
    const msg = input.value.trim();
    if (!msg) return;
    if (!currentSessionId) await createNewSession();
    if (!currentSessionId) return;
    input.value = '';
    input.style.height = 'auto';
    await sendMessage(msg);
}

// ============================================================
// Init
// ============================================================
async function init() {
    agents = await apiListAgents();
    selectedAgent = agents.find(a => a.healthy) || null;
    renderAgentPicker();

    document.getElementById('model-display').textContent = selectedAgent
        ? `Agent (${selectedAgent.agent_name || '?'} · ${selectedAgent.model || '?'})`
        : 'Agent (offline)';
    document.getElementById('model-tag').textContent = selectedAgent?.model || '';

    await loadSessions();
    if (sessions.length === 0) {
        if (selectedAgent) await createNewSession();
    } else {
        await selectSession(sessions[0].id);
    }
}

init();
