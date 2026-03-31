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
            if (msg.thinking) {
                const thinkEl = el.querySelector('.thinking-block') || addThinkingEl(el);
                thinkEl.textContent = msg.thinking;
                thinkEl.style.display = 'block';
            }
            if (msg.tool_calls) {
                const toolsEl = el.querySelector('.tools-container');
                for (const tc of msg.tool_calls) {
                    addToolCallEl(toolsEl, { id: '', name: tc.name, args_summary: JSON.stringify(tc.args).slice(0, 80) });
                    // Mark as completed
                    const blocks = toolsEl.querySelectorAll('.tool-call-block');
                    const last = blocks[blocks.length - 1];
                    if (last) last.querySelector('.tool-status').className = 'tool-status success';
                }
            }
            const respEl = el.querySelector('.response-content');
            if (msg.content) {
                respEl.innerHTML = marked.parse(msg.content);
            }
            if (msg.token_usage) {
                setTokenUsage(el, msg.token_usage);
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

function createAssistantMessageEl() {
    const container = document.getElementById('messages-container');
    const el = document.createElement('div');
    el.className = 'message assistant-message';
    el.innerHTML = `
        <div class="thinking-block" style="display:none;"></div>
        <div class="tools-container"></div>
        <div class="response-content streaming-cursor"></div>
        <div class="token-usage" style="display:none;"></div>
    `;
    container.appendChild(el);
    return el;
}

function addThinkingEl(assistantEl) {
    let el = assistantEl.querySelector('.thinking-block');
    if (!el) {
        el = document.createElement('div');
        el.className = 'thinking-block';
        assistantEl.insertBefore(el, assistantEl.firstChild);
    }
    return el;
}

function addToolCallEl(toolsContainer, data) {
    const el = document.createElement('div');
    const isAdf = data.name && data.name.startsWith('mcp_adf_');
    el.className = `tool-call-block${isAdf ? ' adf-tool' : ''}`;
    el.setAttribute('data-tool-id', data.id || '');
    el.innerHTML = `
        <div class="tool-header">
            <span class="tool-status running"></span>
            <span class="tool-name">${escapeHtml(data.name || '')}</span>
            <span class="tool-args">${escapeHtml(data.args_summary || '')}</span>
            <span class="toggle-result" onclick="toggleToolResult(this)">show</span>
        </div>
        <div class="tool-result"></div>
    `;
    toolsContainer.appendChild(el);
}

function updateToolResultEl(toolsContainer, data) {
    const blocks = toolsContainer.querySelectorAll('.tool-call-block');
    for (const block of blocks) {
        if (block.getAttribute('data-tool-id') === data.id) {
            const status = block.querySelector('.tool-status');
            status.className = `tool-status ${data.is_error ? 'error' : 'success'}`;
            const result = block.querySelector('.tool-result');
            result.textContent = (data.output || '').slice(0, 2000);
            block.classList.add('has-result');
            break;
        }
    }
}

function toggleToolResult(trigger) {
    const block = trigger.closest('.tool-call-block');
    const result = block.querySelector('.tool-result');
    if (result.style.display === 'block') {
        result.style.display = 'none';
        trigger.textContent = 'show';
    } else {
        result.style.display = 'block';
        trigger.textContent = 'hide';
    }
}

function setTokenUsage(assistantEl, data) {
    const el = assistantEl.querySelector('.token-usage');
    if (!el) return;
    const turn = data.turn || {};
    const inp = (turn.input || 0) + (turn.cache_creation || 0) + (turn.cache_read || 0);
    const out = turn.output || 0;
    const cached = turn.cache_read || 0;
    let text = `in:${inp.toLocaleString()} out:${out.toLocaleString()}`;
    if (cached) text += ` cached:${cached.toLocaleString()}`;
    if (data.cost) text += ` ${data.cost}`;
    el.textContent = text;
    el.style.display = 'block';
}

function finalizeAssistantMessage(assistantEl) {
    const resp = assistantEl.querySelector('.response-content');
    if (resp) resp.classList.remove('streaming-cursor');
    const thinking = assistantEl.querySelector('.thinking-block');
    if (thinking) thinking.classList.remove('streaming');
}

// ============================================================
// SSE Streaming
// ============================================================
function parseSSE(buffer) {
    const events = [];
    const lines = buffer.split('\n');
    let remaining = '';
    let currentEvent = null;
    let currentData = '';

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
            currentData = line.slice(6);
        } else if (line === '' && currentEvent) {
            try {
                events.push({ type: currentEvent, data: JSON.parse(currentData) });
            } catch(e) {}
            currentEvent = null;
            currentData = '';
        }
    }
    if (currentEvent) {
        remaining = `event: ${currentEvent}\n`;
        if (currentData) remaining += `data: ${currentData}\n`;
    }
    return { parsed: events, remaining };
}

async function sendMessage(sessionId, message) {
    appendUserMessageEl(message);

    const assistantEl = createAssistantMessageEl();
    const thinkingEl = assistantEl.querySelector('.thinking-block');
    const responseEl = assistantEl.querySelector('.response-content');
    const toolsContainer = assistantEl.querySelector('.tools-container');

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
                handleStreamEvent(event, { thinkingEl, responseEl, toolsContainer, assistantEl });
            }
        }
    } catch (e) {
        if (e.name !== 'AbortError') {
            const errEl = document.createElement('div');
            errEl.style.color = '#ef4444';
            errEl.textContent = `Error: ${e.message}`;
            assistantEl.appendChild(errEl);
        }
    } finally {
        enableInput();
        currentStreamController = null;
        finalizeAssistantMessage(assistantEl);
        flushMarkdown(responseEl);
        scrollToBottom();
    }
}

function handleStreamEvent(event, els) {
    switch (event.type) {
        case 'thinking_start':
            els.thinkingEl.style.display = 'block';
            els.thinkingEl.classList.add('streaming');
            break;

        case 'thinking_delta':
            els.thinkingEl.textContent += event.data.text;
            scrollToBottom();
            break;

        case 'thinking_stop':
            els.thinkingEl.classList.remove('streaming');
            break;

        case 'text_delta':
            markdownBuffer += event.data.text;
            debouncedRenderMarkdown(els.responseEl);
            scrollToBottom();
            break;

        case 'text_stop':
            flushMarkdown(els.responseEl);
            break;

        case 'tool_call':
            addToolCallEl(els.toolsContainer, event.data);
            scrollToBottom();
            break;

        case 'tool_result':
            updateToolResultEl(els.toolsContainer, event.data);
            break;

        case 'token_usage':
            setTokenUsage(els.assistantEl, event.data);
            break;

        case 'done':
            break;

        case 'error':
            const errEl = document.createElement('div');
            errEl.style.color = '#ef4444';
            errEl.textContent = `Error: ${event.data.message}`;
            els.assistantEl.appendChild(errEl);
            break;
    }
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
