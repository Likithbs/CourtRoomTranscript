/**
 * Frontend WebSocket client and UI controller for Transcript Reconciler.
 */

let ws = null;
let currentWords = [];
let currentSeams = [];
let lastSeq = null;
let activeSeamId = null;
let lastRenderedIndex = 0;
const tooltip = document.getElementById("tooltip");

function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws`;
  
  ws = new WebSocket(wsUrl);
  
  ws.onopen = () => {
    console.log("[WS] Connected to Reconciler WebSocket.");
  };
  
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    handleWsMessage(msg);
  };
  
  ws.onclose = () => {
    console.warn("[WS] Disconnected. Reconnecting in 1.5s...");
    setTimeout(connectWebSocket, 1500);
  };
  
  ws.onerror = (err) => {
    console.error("[WS] Error:", err);
  };
}

function requestFullResync() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    console.log("[WS] Sending resync request to server...");
    ws.send(JSON.stringify({ action: "resync" }));
  } else {
    console.log("[WS] Fetching full transcript via REST for resync...");
    fetch("/api/transcript")
      .then((res) => res.json())
      .then((data) => {
        renderTranscript(data.words, data.seams);
      })
      .catch((err) => console.error("[Resync] REST fallback error:", err));
  }
}

function handleWsMessage(msg) {
  const type = msg.event;
  const data = msg.data;
  const seq = msg.seq;

  // Sequence gap detection
  if (seq !== undefined) {
    if (lastSeq !== null && seq > lastSeq + 1) {
      console.warn(`[WS] Gap detected: expected seq ${lastSeq + 1}, received ${seq}. Requesting resync...`);
      requestFullResync();
      return;
    }
    lastSeq = seq;
  }
  
  if (type === "init") {
    activeSeamId = null;
    updateConnectionState(data.connection_state, data.message);
    updateMetrics(data.stats);
    renderTranscript(data.transcript, data.resolved_seams, false);
    if (data.feeder_logs && data.feeder_logs.length > 0) {
      const box = document.getElementById("feeder-log-box");
      if (box) {
        box.textContent = "";
        data.feeder_logs.forEach(l => appendFeederLog(l));
      }
    }
  } else if (type === "connection_state") {
    updateConnectionState(data.state, data.message);
    if (data.stats) updateMetrics(data.stats);
  } else if (type === "packet_ingested") {
    updateMetrics(data.stats);
    if (data.new_words && data.new_words.length > 0) {
      appendTranscriptWords(data.new_words);
    }
  } else if (type === "feeder_log") {
    appendFeederLog(data.log);
  } else if (type === "transcript_update") {
    // Policy change: force full reset and re-render
    updateMetrics(data.stats);
    renderTranscript(data.transcript, data.resolved_seams, true);
  } else if (type === "resync") {
    updateMetrics(data.stats);
    renderTranscript(data.transcript, data.resolved_seams, true);
  } else if (type === "reset") {
    updateMetrics(data.stats);
    renderTranscript([], [], true);
    const box = document.getElementById("feeder-log-box");
    if (box) box.textContent = "No feeder process logs yet.";
  }
}

function appendFeederLog(logText) {
  const box = document.getElementById("feeder-log-box");
  if (!box) return;
  if (box.textContent === "No feeder process logs yet.") {
    box.textContent = "";
  }
  const lineEl = document.createElement("div");
  lineEl.textContent = logText;
  box.appendChild(lineEl);
  box.scrollTop = box.scrollHeight;
}

function updateConnectionState(state, message) {
  const badge = document.getElementById("conn-badge");
  const linkState = document.getElementById("link-state");
  const eventMsg = document.getElementById("event-msg");
  
  const upperState = (state || "DISCONNECTED").toUpperCase();
  badge.textContent = upperState;
  linkState.textContent = upperState;
  
  badge.className = "status-badge";
  if (state === "receiving") {
    badge.classList.add("state-receiving");
  } else if (state === "connected") {
    badge.classList.add("state-connected");
  } else if (state === "done") {
    badge.classList.add("state-done");
  } else if (state === "dropped") {
    badge.classList.add("state-dropped");
  } else {
    badge.classList.add("state-disconnected");
  }
  
  if (message) {
    eventMsg.textContent = message;
  }
}

function updateMetrics(stats) {
  if (!stats) return;
  
  const expChunks = (stats.expected_chunks !== undefined && stats.expected_chunks !== null && stats.expected_chunks > 0) ? stats.expected_chunks : "--";
  const expSeams = (stats.expected_seams !== undefined && stats.expected_seams !== null && stats.expected_seams > 0) ? stats.expected_seams : "--";

  document.getElementById("chunks-curr").textContent = stats.chunks_successful || 0;
  document.getElementById("chunks-total").textContent = expChunks;
  document.getElementById("chunks-progress").style.width = `${stats.chunks_complete_pct || 0}%`;
  
  document.getElementById("seams-curr").textContent = stats.seams_resolved || 0;
  document.getElementById("seams-total").textContent = expSeams;
  document.getElementById("seams-progress").style.width = `${stats.seams_complete_pct || 0}%`;
  
  document.getElementById("packets-total").textContent = stats.packets_received || 0;
  document.getElementById("chunks-failed").textContent = stats.chunks_failed || 0;
  document.getElementById("duplicates-ignored").textContent = stats.duplicates_ignored || 0;
  document.getElementById("words-aligned").textContent = stats.seam_aligned_words || 0;
  document.getElementById("words-suppressed").textContent = stats.seam_suppressed_words || 0;
  document.getElementById("words-total").textContent = stats.total_reconciled_words || 0;
  
  const summaryEl = document.getElementById("stream-summary");
  if (stats.chunks_successful > 0) {
    summaryEl.textContent = `(${stats.chunks_successful}/${expChunks} chunks, ${stats.total_reconciled_words} words)`;
  } else {
    summaryEl.textContent = "(Waiting for stream...)";
  }
}

function appendTranscriptWords(newWords) {
  if (!newWords || newWords.length === 0) return;
  
  const container = document.getElementById("transcript-view");
  const emptyNotice = document.getElementById("empty-notice");
  if (emptyNotice) {
    emptyNotice.remove();
  }
  
  let html = "";
  for (let i = 0; i < newWords.length; i++) {
    const w = newWords[i];
    
    // Check if this word starts a new seam
    if (w.is_seam_resolved && w.seam_id && w.seam_id !== activeSeamId) {
      activeSeamId = w.seam_id;
      html += `<div class="seam-divider">&#9889; ${escapeHtml(w.seam_id)} &bull; ${escapeHtml(w.resolution_note || "Reconciled overlap boundary")}</div>`;
    } else if (!w.is_seam_resolved && activeSeamId !== null) {
      activeSeamId = null;
    }
    
    // Word class
    let cls = "word-token";
    if (w.is_seam_resolved) {
      cls += " word-seam";
    } else if (w.chunk_id % 2 === 0) {
      cls += " word-c-even";
    } else {
      cls += " word-c-odd";
    }
    
    const startSec = typeof w.start === "number" ? w.start.toFixed(3) : w.start;
    const endSec = typeof w.end === "number" ? w.end.toFixed(3) : w.end;
    const info = `Chunk: C${w.chunk_id} | [${startSec}s - ${endSec}s]${w.is_seam_resolved ? ' | SEAM: ' + (w.seam_id || '') : ''}`;
    
    html += `<span class="${cls}" data-info="${escapeHtml(info)}" onmouseenter="showTooltip(event)" onmouseleave="hideTooltip()">${escapeHtml(w.text)}</span> `;
    currentWords.push(w);
  }
  
  container.insertAdjacentHTML("beforeend", html);
  lastRenderedIndex = currentWords.length;
  
  // Auto-scroll if enabled
  const autoScroll = document.getElementById("auto-scroll").checked;
  if (autoScroll) {
    container.scrollTop = container.scrollHeight;
  }
}

function renderTranscript(words, seams, forceFullReset = false) {
  currentSeams = seams || [];
  const container = document.getElementById("transcript-view");
  if (!container) return;
  
  if (forceFullReset || !words || words.length === 0) {
    lastRenderedIndex = 0;
    currentWords = [];
    activeSeamId = null;
    if (!words || words.length === 0) {
      container.innerHTML = `
        <div id="empty-notice" style="color:#94a3b8; text-align:center; padding-top:60px;">
          No transcript chunks received yet.<br>Click <strong>Start Feeder</strong> or run <code>python feeder.py</code> in terminal to begin.
        </div>
      `;
      return;
    }
    container.innerHTML = "";
  } else {
    const emptyNotice = document.getElementById("empty-notice");
    if (emptyNotice) {
      emptyNotice.remove();
    }
  }
  
  // If words array shrunk, clear and reset
  if (words.length < lastRenderedIndex) {
    container.innerHTML = "";
    lastRenderedIndex = 0;
    activeSeamId = null;
  }
  
  // If nothing new beyond lastRenderedIndex, return early
  if (lastRenderedIndex >= words.length) {
    currentWords = words ? [...words] : [];
    return;
  }
  
  let html = "";
  for (let i = lastRenderedIndex; i < words.length; i++) {
    const w = words[i];
    
    // Check if this word starts a new seam
    if (w.is_seam_resolved && w.seam_id && w.seam_id !== activeSeamId) {
      activeSeamId = w.seam_id;
      html += `<div class="seam-divider">&#9889; ${escapeHtml(w.seam_id)} &bull; ${escapeHtml(w.resolution_note || "Reconciled overlap boundary")}</div>`;
    } else if (!w.is_seam_resolved && activeSeamId !== null) {
      activeSeamId = null;
    }
    
    // Word class
    let cls = "word-token";
    if (w.is_seam_resolved) {
      cls += " word-seam";
    } else if (w.chunk_id % 2 === 0) {
      cls += " word-c-even";
    } else {
      cls += " word-c-odd";
    }
    
    const startSec = typeof w.start === "number" ? w.start.toFixed(3) : w.start;
    const endSec = typeof w.end === "number" ? w.end.toFixed(3) : w.end;
    const info = `Chunk: C${w.chunk_id} | [${startSec}s - ${endSec}s]${w.is_seam_resolved ? ' | SEAM: ' + (w.seam_id || '') : ''}`;
    
    html += `<span class="${cls}" data-info="${escapeHtml(info)}" onmouseenter="showTooltip(event)" onmouseleave="hideTooltip()">${escapeHtml(w.text)}</span> `;
  }
  
  container.insertAdjacentHTML("beforeend", html);
  lastRenderedIndex = words.length;
  currentWords = words ? [...words] : [];
  
  // Auto-scroll if enabled
  const autoScroll = document.getElementById("auto-scroll").checked;
  if (autoScroll) {
    container.scrollTop = container.scrollHeight;
  }
}

function showTooltip(e) {
  const info = e.target.getAttribute("data-info");
  if (!info) return;
  tooltip.textContent = info;
  tooltip.style.display = "block";
  tooltip.style.left = `${e.clientX + 12}px`;
  tooltip.style.top = `${e.clientY + 12}px`;
}

function hideTooltip() {
  tooltip.style.display = "none";
}

function escapeHtml(str) {
  if (!str) return "";
  return str.toString()
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function startFeeder() {
  const delay = parseFloat(document.getElementById("feed-delay").value);
  const btn = document.getElementById("btn-start-feeder");
  btn.disabled = true;
  btn.textContent = "Launching Feeder...";
  
  try {
    const res = await fetch("/api/feeder/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ delay })
    });
    const data = await res.json();
    console.log("[Feeder] Launch result:", data);
    btn.textContent = "Feeder Running...";
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Restart Feeder";
    }, 2000);
  } catch (err) {
    console.error("[Feeder] Failed to start:", err);
    btn.disabled = false;
    btn.textContent = "Start Feeder (Error)";
  }
}

async function resetReconciler() {
  if (!confirm("Are you sure you want to reset the transcript and reconciler state?")) {
    return;
  }
  try {
    await fetch("/api/reset", { method: "POST" });
  } catch (err) {
    console.error("[Reset] Error:", err);
  }
}

async function updatePolicy() {
  const bias = document.getElementById("policy-select").value;
  try {
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bias })
    });
    console.log("[Policy] Changed to:", bias);
  } catch (err) {
    console.error("[Policy] Error:", err);
  }
}

// Initial connection
window.addEventListener("DOMContentLoaded", () => {
  connectWebSocket();
});
