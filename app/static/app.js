/**
 * Expense Ledger Agent - Client-side JavaScript
 *
 * Handles: model selection, chat messaging, session management,
 * tool trace display, metrics, and new-chat functionality.
 */

// --- Session management ---
let sessionId = generateSessionId();

function generateSessionId() {
  // 20-char hex string (satisfies min_length=16)
  return Array.from(crypto.getRandomValues(new Uint8Array(10)))
    .map(b => b.toString(16).padStart(2, '0')).join('');
}

// --- DOM references ---
const modelSelect = document.getElementById('model');
const messagesDiv = document.getElementById('messages');
const chatForm = document.getElementById('chat');
const taskInput = document.getElementById('task');
const sendBtn = document.getElementById('send');
const resetBtn = document.getElementById('reset');
const statusEl = document.getElementById('status');
const memoryEl = document.getElementById('memory');
const traceEl = document.getElementById('trace');
const observationsEl = document.getElementById('observations');
const metricsEl = document.getElementById('metrics');
const externalInput = document.getElementById('external');

let messageCount = 0;

// --- Load available models ---
async function loadModels() {
  try {
    const res = await fetch('/models');
    const data = await res.json();
    modelSelect.innerHTML = '';
    (data.models || ['unconfigured']).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      modelSelect.appendChild(opt);
    });
  } catch (e) {
    modelSelect.innerHTML = '<option>unconfigured</option>';
  }
}

// --- Add message to chat ---
function addMessage(text, role) {
  // Remove welcome message on first interaction
  const welcome = messagesDiv.querySelector('.welcome-msg');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.textContent = text;
  messagesDiv.appendChild(div);
  messagesDiv.scrollTop = messagesDiv.scrollHeight;
  messageCount++;
  memoryEl.textContent = `${messageCount} messages in this chat`;
}

// --- Update status badge ---
function setStatus(text, state) {
  statusEl.textContent = text;
  statusEl.className = 'status-badge';
  if (state) statusEl.classList.add(state);
}

// --- Render tool traces ---
function renderTraces(toolCalls) {
  if (!toolCalls || toolCalls.length === 0) {
    traceEl.innerHTML = '<p class="empty-state">No tool calls yet.</p>';
    return;
  }
  traceEl.innerHTML = toolCalls.map(tc => {
    const dotClass = tc.outcome === 'success' ? 'success' :
                     tc.outcome === 'timeout' ? 'timeout' : 'error';
    return `<div class="trace-item">
      <span class="trace-dot ${dotClass}"></span>
      <span>Step ${tc.step}: <strong>${tc.tool}</strong> (${tc.outcome})</span>
      <span style="color:var(--text-muted);margin-left:auto">${tc.latency_ms?.toFixed(0) || 0}ms</span>
    </div>`;
  }).join('');
}

// --- Render observations ---
function renderObservations(events) {
  if (!events || events.length === 0) {
    observationsEl.innerHTML = '<p class="empty-state">No observations yet.</p>';
    return;
  }
  observationsEl.innerHTML = events.map(ev => {
    const text = ev.result_preview || ev.reason || ev.event || JSON.stringify(ev);
    return `<div class="trace-item">
      <span style="color:var(--text-muted)">Step ${ev.step || '?'}:</span>
      <span>${escapeHtml(String(text).substring(0, 150))}</span>
    </div>`;
  }).join('');
}

// --- Render metrics ---
function renderMetrics(metrics, steps, status) {
  if (!metrics) {
    metricsEl.innerHTML = '<p class="empty-state">No metrics yet.</p>';
    return;
  }
  metricsEl.innerHTML = `<div class="metrics-grid">
    <div class="metric-item">
      <div class="metric-value">${steps || 0}</div>
      <div class="metric-label">Steps</div>
    </div>
    <div class="metric-item">
      <div class="metric-value">${metrics.model_calls || 0}</div>
      <div class="metric-label">LLM Calls</div>
    </div>
    <div class="metric-item">
      <div class="metric-value">${metrics.latency_ms ? (metrics.latency_ms / 1000).toFixed(1) + 's' : '-'}</div>
      <div class="metric-label">Latency</div>
    </div>
    <div class="metric-item">
      <div class="metric-value">${(metrics.input_tokens || 0) + (metrics.output_tokens || 0)}</div>
      <div class="metric-label">Tokens</div>
    </div>
  </div>
  <div style="margin-top:0.5rem;font-size:0.75rem;color:var(--text-muted)">
    Status: <strong style="color:${status === 'completed' ? 'var(--success)' : status === 'failed' ? 'var(--error)' : 'var(--warning)'}">${status}</strong>
  </div>`;
}

// --- Escape HTML ---
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// --- Send message ---
chatForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const task = taskInput.value.trim();
  if (!task) return;

  addMessage(task, 'user');
  taskInput.value = '';

  // Build external_context if provided
  const externalContext = [];
  const extText = externalInput.value.trim();
  if (extText) {
    externalContext.push({
      source: 'user_note',
      content: extText,
      trust: 'untrusted'
    });
  }

  // Disable form while processing
  sendBtn.disabled = true;
  sendBtn.querySelector('.btn-text').style.display = 'none';
  sendBtn.querySelector('.btn-loading').style.display = 'inline';
  setStatus('Processing...', 'running');

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        task: task,
        model: modelSelect.value,
        external_context: externalContext,
        arena_config: { max_steps: 6, fault: 'none' }
      })
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();

    // Display response
    addMessage(data.final_response, 'assistant');

    // Update sidebar
    const statusState = ['completed'].includes(data.status) ? 'completed' :
                         ['failed', 'contract_error', 'tool_error'].includes(data.status) ? 'error' : '';
    setStatus(data.status + (data.stop_reason ? ` (${data.stop_reason})` : ''), statusState);

    renderTraces(data.tool_calls);
    renderObservations(data.events);
    renderMetrics(data.metrics, data.steps, data.status);

  } catch (err) {
    addMessage(`Error: ${err.message}`, 'system');
    setStatus('Error', 'error');
  } finally {
    sendBtn.disabled = false;
    sendBtn.querySelector('.btn-text').style.display = 'inline';
    sendBtn.querySelector('.btn-loading').style.display = 'none';
  }
});

// --- Reset chat ---
resetBtn.addEventListener('click', async () => {
  try {
    await fetch(`/chat/${sessionId}`, { method: 'DELETE' });
  } catch (e) { /* ignore */ }

  sessionId = generateSessionId();
  messageCount = 0;
  messagesDiv.innerHTML = `<div class="welcome-msg">
    <p><strong>Welcome!</strong> Send expense data (receipt text or CSV) and I'll categorize it into a sandbox ledger.</p>
    <p class="hint">Example: "Coffee at Starbucks $4.50, Uber ride $12.00, Lunch $11.25"</p>
  </div>`;
  memoryEl.textContent = '0 messages in this chat';
  traceEl.innerHTML = '<p class="empty-state">No tool calls yet.</p>';
  observationsEl.innerHTML = '<p class="empty-state">No observations yet.</p>';
  metricsEl.innerHTML = '<p class="empty-state">No metrics yet.</p>';
  setStatus('Idle', '');
  externalInput.value = '';
});

// --- Init ---
loadModels();
