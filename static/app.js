/* ── iRacing Adaptive Settings Optimizer — app.js ────────────────────────── */

"use strict";

let _sseSource = null;

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  loadReplays();
  loadProfiles();
  loadCurrentSettings();
  checkRunningState();
  loadCalibrationStatus();
});

// ── API helpers ───────────────────────────────────────────────────────────────

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, opts);
  return res.json();
}

// ── Data loaders ──────────────────────────────────────────────────────────────

async function loadReplays() {
  const sel = document.getElementById("replay-select");
  const hint = document.getElementById("replay-hint");
  try {
    const data = await apiFetch("/api/replays");
    sel.innerHTML = '<option value="">-- select a replay --</option>';
    if (data.replays && data.replays.length > 0) {
      window._replayList = data.replays;
      data.replays.forEach(r => {
        const opt = document.createElement("option");
        opt.value = r.path;
        opt.textContent = `${r.name}  (${r.size_mb} MB)`;
        sel.appendChild(opt);
      });
      hint.textContent = `${data.replays.length} replay(s) found`;
    } else {
      window._replayList = [];
      hint.textContent = "No .rpy files found in Documents\\iRacing\\replay\\";
    }
    if (data.error) {
      hint.textContent = `Error: ${data.error}`;
    }
    // Populate calibration replay dropdown with same list
    const calSel = document.getElementById("cal-replay-select");
    if (calSel) {
      calSel.innerHTML = '<option value="">-- select a replay --</option>';
      (window._replayList || []).forEach(r => {
        const opt = document.createElement("option");
        opt.value = r.path;
        opt.textContent = `${r.name}  (${r.size_mb} MB)`;
        calSel.appendChild(opt);
      });
    }
  } catch (e) {
    hint.textContent = `Failed to load replays: ${e.message}`;
  }
}

async function loadProfiles() {
  const container = document.getElementById("profiles-list");
  if (!container) return;
  try {
    const data = await apiFetch("/api/profiles");
    renderProfiles(data.profiles || []);
  } catch (e) {
    container.innerHTML = `<div class="no-profiles">Failed to load profiles: ${e.message}</div>`;
  }
}

function renderProfiles(profiles) {
  const container = document.getElementById("profiles-list");
  if (!container) return;
  if (!profiles || profiles.length === 0) {
    container.innerHTML = '<div class="no-profiles">No saved profiles yet.</div>';
    return;
  }
  container.innerHTML = profiles.map(p => {
    const fpsDisplay = p.fps_median != null ? `${p.fps_median.toFixed(1)} fps median` : "";
    const created = p.created ? p.created.replace("T", " ").slice(0, 16) : "";
    return `
      <div class="profile-row">
        <span class="profile-name">${esc(p.name)}</span>
        <span class="profile-meta">
          <span class="accent">${p.target_fps || "?"} FPS target</span>
          ${fpsDisplay ? ` &bull; ${esc(fpsDisplay)}` : ""}
          ${p.scenario ? ` &bull; ${esc(p.scenario)}` : ""}
          ${created ? ` &bull; ${esc(created)}` : ""}
        </span>
        <button class="btn btn-sm" onclick="applyProfile(${JSON.stringify(p.name)})">Apply</button>
      </div>
    `;
  }).join("");
}

async function loadCurrentSettings() {
  const iniInput = document.getElementById("ini-path");
  try {
    const data = await apiFetch("/api/settings");
    if (data.renderer_ini) {
      iniInput.value = data.renderer_ini;
    }
    if (data.error) {
      iniInput.value = data.error;
    }
  } catch (e) {
    iniInput.value = `Error: ${e.message}`;
  }
}

// ── Benchmark control ─────────────────────────────────────────────────────────

async function startBenchmark() {
  const replayPath = document.getElementById("replay-select").value;
  if (!replayPath) {
    alert("Please select a replay file.");
    return;
  }

  const selectedFps = document.querySelector('input[name="fps"]:checked');
  let targetFps = 120;
  if (selectedFps) {
    if (selectedFps.value === "custom") {
      targetFps = parseInt(document.getElementById("fps-custom").value, 10) || 120;
    } else {
      targetFps = parseInt(selectedFps.value, 10);
    }
  }

  const mock = document.getElementById("mock-mode").checked;

  const payload = { target_fps: targetFps, replay: replayPath, mock };

  try {
    const data = await apiFetch("/api/benchmark/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (data.error) {
      alert(`Could not start: ${data.error}`);
      return;
    }

    const staleWarning = document.getElementById("stale-cal-warning");
    if (staleWarning) {
      staleWarning.style.display = data.stale_calibration_warning === true ? "" : "none";
    }

    showPanel("progress");
    resetProgress();
    appendLog(`Benchmark started — target: ${targetFps} FPS${mock ? " (mock mode)" : ""}`, "");
    startSSE();

  } catch (e) {
    alert(`Request failed: ${e.message}`);
  }
}

async function stopBenchmark() {
  if (!confirm("Abort the current benchmark run?")) return;
  try {
    await apiFetch("/api/benchmark/stop", { method: "POST" });
    appendLog("Abort requested.", "log-warn");
  } catch (e) {
    appendLog(`Abort request failed: ${e.message}`, "log-error");
  }
}

async function checkRunningState() {
  try {
    const data = await apiFetch("/api/status");
    if (data.status === "running") {
      showPanel("progress");
      resetProgress();
      appendLog("Reconnected to running benchmark.", "");
      startSSE();
    } else if (data.status === "done" || data.status === "done_partial") {
      await loadResult();
      showPanel("results");
    }
  } catch (_) {
    // ignore on init
  }
}

// ── SSE stream ────────────────────────────────────────────────────────────────

function startSSE() {
  if (_sseSource) {
    _sseSource.close();
    _sseSource = null;
  }

  _sseSource = new EventSource("/api/benchmark/stream");

  _sseSource.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    handleSSEEvent(ev);
  };

  _sseSource.onerror = () => {
    appendLog("Stream disconnected.", "log-warn");
    _sseSource.close();
    _sseSource = null;
  };
}

function handleSSEEvent(ev) {
  const type = ev.type || "";

  switch (type) {
    case "log":
      appendLog(ev.msg || "");
      // Parse live FPS from sampler log lines
      parseLiveFps(ev.msg || "");
      break;

    case "progress": {
      const pct = ev.pct != null ? ev.pct : 0;
      setProgress(pct);
      if (ev.msg) appendLog(ev.msg);
      break;
    }

    case "setting_start":
      setCurrentSetting(ev.display_name || ev.key || "—", ev.value != null ? ev.value : "—");
      if (ev.msg) appendLog(ev.msg);
      break;

    case "setting_done":
      if (ev.msg) appendLog(ev.msg);
      break;

    case "fps":
      // Explicit FPS event from sampler
      setLiveFps(ev.fps != null ? ev.fps : null);
      break;

    case "done":
      setProgress(100);
      appendLog(`Benchmark complete. Success: ${ev.success ? "YES" : "PARTIAL"}`, "");
      setCurrentSetting("Done", "");
      _sseSource && _sseSource.close();
      _sseSource = null;
      // Populate calibration row in results if data present
      if (ev.correction_factor != null) {
        const cfEl = document.getElementById("result-correction-factor");
        const atEl = document.getElementById("result-adjusted-target");
        const rowEl = document.getElementById("result-calibration-row");
        if (cfEl) cfEl.textContent = `${ev.correction_factor.toFixed(2)}×`;
        if (atEl) atEl.textContent = ev.adjusted_target_fps != null ? `${ev.adjusted_target_fps} fps` : "—";
        if (rowEl) rowEl.style.display = "flex";
      }
      setTimeout(async () => {
        await loadResult();
        await loadProfiles();
        await loadCalibrationStatus();
        showPanel("results");
      }, 600);
      break;

    case "error":
      appendLog(`ERROR: ${ev.msg || "Unknown error"}`, "log-error");
      document.getElementById("btn-start").disabled = false;
      document.getElementById("btn-abort").disabled = true;
      _sseSource && _sseSource.close();
      _sseSource = null;
      break;

    case "aborted":
      appendLog("Benchmark aborted.", "log-warn");
      document.getElementById("btn-start").disabled = false;
      _sseSource && _sseSource.close();
      _sseSource = null;
      break;
  }
}

// ── Result rendering ──────────────────────────────────────────────────────────

async function loadResult() {
  try {
    const data = await apiFetch("/api/benchmark/result");
    renderResult(data);
  } catch (e) {
    appendLog(`Failed to load results: ${e.message}`, "log-error");
  }
}

function renderResult(data) {
  const summaryEl = document.getElementById("results-summary");
  const fps = data.fps_stats || {};
  const success = data.success;

  summaryEl.innerHTML = `
    <div class="stat-card ${success ? "stat-success" : "stat-fail"}">
      <span class="stat-value">${success ? "PASS" : "PARTIAL"}</span>
      <span class="stat-label">Result</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${fps.median != null ? fps.median.toFixed(1) : "—"}</span>
      <span class="stat-label">FPS Median</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${fps.p5 != null ? fps.p5.toFixed(1) : "—"}</span>
      <span class="stat-label">FPS p5 (floor)</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${fps.p95 != null ? fps.p95.toFixed(1) : "—"}</span>
      <span class="stat-label">FPS p95</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${data.target_fps || "—"}</span>
      <span class="stat-label">Target FPS</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${data.iterations != null ? data.iterations : "—"}</span>
      <span class="stat-label">Iterations</span>
    </div>
    <div class="stat-card">
      <span class="stat-value">${data.duration_seconds != null ? formatDuration(data.duration_seconds) : "—"}</span>
      <span class="stat-label">Duration</span>
    </div>
  `;

  // Settings comparison table
  const tbody = document.getElementById("settings-tbody");
  const comparison = data.comparison || [];
  if (comparison.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" style="color: var(--text-dim); padding: 1rem;">No comparison data available.</td></tr>';
  } else {
    tbody.innerHTML = comparison.map(row => {
      const changed = row.changed;
      const changeCell = changed
        ? `<td class="changed-badge">Changed</td>`
        : `<td class="no-change">—</td>`;
      return `
        <tr class="${changed ? "changed" : ""}">
          <td>${esc(row.display_name || row.key)}</td>
          <td>${row.original != null ? row.original : "<span style='color:var(--text-dim)'>—</span>"}</td>
          <td>${row.optimized != null ? row.optimized : "<span style='color:var(--text-dim)'>—</span>"}</td>
          ${changeCell}
        </tr>
      `;
    }).join("");
  }

  // Pre-fill profile name
  if (data.target_fps) {
    const nameInput = document.getElementById("profile-name");
    if (!nameInput.value) {
      nameInput.value = `${data.target_fps}fps Optimized`;
    }
  }
}

// ── Profile actions ───────────────────────────────────────────────────────────

async function saveProfile() {
  const name = document.getElementById("profile-name").value.trim();
  const scenario = document.getElementById("profile-scenario").value;
  const msgEl = document.getElementById("save-msg");

  if (!name) {
    showSaveMsg("Profile name is required.", false);
    return;
  }

  try {
    const data = await apiFetch("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, scenario }),
    });

    if (data.error) {
      showSaveMsg(`Error: ${data.error}`, false);
    } else {
      showSaveMsg(`Profile "${name}" saved.`, true);
      loadProfiles();
    }
  } catch (e) {
    showSaveMsg(`Request failed: ${e.message}`, false);
  }
}

async function applyProfile(name) {
  if (!confirm(`Apply profile "${name}" to rendererDX11.ini?`)) return;
  try {
    const data = await apiFetch(`/api/profiles/${encodeURIComponent(name)}/apply`, {
      method: "POST",
    });
    if (data.error) {
      alert(`Could not apply profile: ${data.error}`);
    } else {
      alert(`Profile "${name}" applied successfully.`);
      loadCurrentSettings();
    }
  } catch (e) {
    alert(`Request failed: ${e.message}`);
  }
}

function showSaveMsg(msg, ok) {
  const el = document.getElementById("save-msg");
  el.textContent = msg;
  el.className = "save-msg " + (ok ? "ok" : "err");
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 5000);
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function showPanel(name) {
  const panels = { setup: "panel-setup", progress: "panel-progress", results: "panel-results" };
  const startBtn = document.getElementById("btn-start");
  const abortBtn = document.getElementById("btn-abort");

  // Show/hide panels
  document.getElementById("panel-setup").classList.remove("hidden");

  if (name === "progress") {
    document.getElementById("panel-progress").classList.remove("hidden");
    document.getElementById("panel-results").classList.add("hidden");
    if (startBtn) startBtn.disabled = true;
    if (abortBtn) abortBtn.disabled = false;
  } else if (name === "results") {
    document.getElementById("panel-progress").classList.add("hidden");
    document.getElementById("panel-results").classList.remove("hidden");
    if (startBtn) startBtn.disabled = false;
    if (abortBtn) abortBtn.disabled = true;
  } else {
    // idle/setup
    document.getElementById("panel-progress").classList.add("hidden");
    document.getElementById("panel-results").classList.add("hidden");
    if (startBtn) startBtn.disabled = false;
  }
}

function resetToSetup() {
  showPanel("setup");
  resetProgress();
  clearLog();
  setCurrentSetting("—", "");
  setLiveFps(null);
}

function resetProgress() {
  setProgress(0);
  setCurrentSetting("—", "");
  setLiveFps(null);
  const startBtn = document.getElementById("btn-start");
  const abortBtn = document.getElementById("btn-abort");
  if (startBtn) startBtn.disabled = true;
  if (abortBtn) abortBtn.disabled = false;
}

function setProgress(pct) {
  const fill = document.getElementById("progress-fill");
  const label = document.getElementById("progress-label");
  const clamped = Math.max(0, Math.min(100, pct));
  if (fill) fill.style.width = `${clamped}%`;
  if (label) label.textContent = `${Math.round(clamped)}%`;
}

function setCurrentSetting(name, value) {
  const el = document.getElementById("current-setting");
  if (!el) return;
  if (value !== "" && value != null) {
    el.textContent = `${name} = ${value}`;
  } else {
    el.textContent = name || "—";
  }
}

function setLiveFps(fps) {
  const el = document.getElementById("live-fps");
  if (!el) return;
  el.textContent = fps != null ? `${Number(fps).toFixed(1)}` : "—";
}

function parseLiveFps(msg) {
  // Extract FPS from log lines like "current FPS: 143.2" or "current: 143.2 fps"
  const m = msg.match(/current(?:\s+fps)?[:\s]+([0-9]+(?:\.[0-9]+)?)/i);
  if (m) {
    setLiveFps(parseFloat(m[1]));
  }
}

function appendLog(msg, cssClass) {
  const out = document.getElementById("log-output");
  if (!out) return;
  const line = document.createElement("div");
  line.className = "log-line" + (cssClass ? " " + cssClass : "");
  const ts = new Date().toLocaleTimeString("en-US", { hour12: false });
  line.textContent = `[${ts}] ${msg}`;
  out.appendChild(line);
  // Auto-scroll to bottom
  out.scrollTop = out.scrollHeight;
}

function clearLog() {
  const out = document.getElementById("log-output");
  if (out) out.innerHTML = "";
}

function formatDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

function esc(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Calibration ────────────────────────────────────────────────────────────────

let _calSSE = null;

function toggleCalibratePanel() {
  const body = document.getElementById("cal-panel-body");
  const arrow = document.getElementById("cal-arrow");
  if (!body) return;
  const isOpen = body.style.display !== "none";
  body.style.display = isOpen ? "none" : "";
  if (arrow) arrow.style.transform = isOpen ? "" : "rotate(90deg)";
}

function expandCalibratePanel() {
  const body = document.getElementById("cal-panel-body");
  const arrow = document.getElementById("cal-arrow");
  if (!body) return;
  body.style.display = "";
  if (arrow) arrow.style.transform = "rotate(90deg)";
}

async function loadCalibrationStatus() {
  try {
    const data = await apiFetch("/api/calibrate/status");
    renderCalibrationStatus(data);
  } catch (e) {
    // Non-fatal — calibration feature may not be deployed yet
  }
}

function renderCalibrationStatus(data) {
  const el = document.getElementById("cal-status-line");
  if (!el) return;

  if (data.correction_factor == null) {
    el.textContent = "No calibration — optimizer using replay FPS directly (correction factor: 1.0×)";
    el.style.color = "var(--text-dim)";
  } else {
    const pct = Math.round(data.correction_factor * 100);
    const factor = data.correction_factor.toFixed(2);
    const liveP5 = data.live_fps_p5 != null ? data.live_fps_p5.toFixed(1) : "?";
    const replayP5 = data.replay_fps_p5 != null ? data.replay_fps_p5.toFixed(1) : "?";
    if (data.valid) {
      el.textContent = `Calibrated — Live FPS is ${pct}% of replay (factor: ${factor}×). Live p5: ${liveP5} fps vs Replay p5: ${replayP5} fps.`;
      el.style.color = "var(--success)";
    } else {
      el.textContent = `Calibration data present (factor: ${factor}×) but marked invalid.`;
      el.style.color = "#e8b800";
    }
  }

  // Sync cal replay dropdown if replays already loaded
  const calSel = document.getElementById("cal-replay-select");
  if (calSel && window._replayList && window._replayList.length > 0 && calSel.options.length <= 1) {
    calSel.innerHTML = '<option value="">-- select a replay --</option>';
    window._replayList.forEach(r => {
      const opt = document.createElement("option");
      opt.value = r.path;
      opt.textContent = `${r.name}  (${r.size_mb} MB)`;
      calSel.appendChild(opt);
    });
  }
}

async function startCalibration() {
  const calSel = document.getElementById("cal-replay-select");
  const replay = calSel ? calSel.value : "";
  if (!replay) {
    alert("Please select a replay file for the baseline.");
    return;
  }

  const mock = document.getElementById("mock-mode") ? document.getElementById("mock-mode").checked : false;

  try {
    const res = await fetch("/api/calibrate/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ replay, mock }),
    });

    if (res.status === 409) {
      alert("Calibration is already running.");
      return;
    }

    const data = await res.json();
    if (data.error) {
      alert(`Could not start calibration: ${data.error}`);
      return;
    }

    document.getElementById("btn-cal-start").style.display = "none";
    document.getElementById("btn-cal-stop").style.display = "";
    document.getElementById("cal-log").style.display = "";
    document.getElementById("cal-phase").style.display = "";
    document.getElementById("cal-log").textContent = "";
    expandCalibratePanel();
    startCalibrationSSE();

  } catch (e) {
    alert(`Calibration request failed: ${e.message}`);
  }
}

async function stopCalibration() {
  try {
    await apiFetch("/api/calibrate/stop", { method: "POST" });
    appendCalLog("Stop requested — waiting for confirmation...");
  } catch (e) {
    appendCalLog(`Stop request failed: ${e.message}`);
  }
}

async function clearCalibration() {
  try {
    await fetch("/api/calibrate/clear", { method: "POST" });
    renderCalibrationStatus({ correction_factor: null, valid: null });
    appendCalLog("Calibration data cleared.");
  } catch (e) {
    appendCalLog(`Clear request failed: ${e.message}`);
  }
}

function startCalibrationSSE() {
  if (_calSSE) { _calSSE.close(); }
  _calSSE = new EventSource("/api/calibrate/stream");
  _calSSE.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    handleCalibrationEvent(ev);
  };
  _calSSE.onerror = () => {
    appendCalLog("Connection lost.");
    resetCalUI();
  };
}

function handleCalibrationEvent(event) {
  const TERMINAL = ["cal_done", "cal_error", "cal_aborted"];

  switch (event.type) {
    case "cal_phase1_start":
      document.getElementById("cal-phase-label").textContent = "Phase 1: Replay Baseline";
      document.getElementById("cal-phase").style.display = "";
      appendCalLog(event.msg);
      break;

    case "log":
      appendCalLog(event.msg);
      break;

    case "cal_phase1_done":
      appendCalLog(`Replay baseline: p5=${event.fps_p5.toFixed(1)} fps, median=${event.fps_median.toFixed(1)} fps`);
      break;

    case "cal_waiting":
      document.getElementById("cal-phase-label").textContent = "Phase 2: Waiting for Live Session";
      appendCalLog(event.msg);
      break;

    case "cal_phase2_start":
      document.getElementById("cal-phase-label").textContent = "Phase 2: Collecting Live FPS";
      document.getElementById("cal-track").textContent = event.track;
      document.getElementById("cal-car").textContent = event.car;
      document.getElementById("cal-collection-stats").style.display = "";
      appendCalLog(`Live session detected: ${event.track} — ${event.car}`);
      break;

    case "cal_progress":
      document.getElementById("cal-samples").textContent = event.sample_count;
      document.getElementById("cal-fps-p5").textContent = event.fps_p5_so_far.toFixed(1);
      break;

    case "cal_done":
      appendCalLog(`Calibration complete! Correction factor: ${event.correction_factor.toFixed(3)}×`);
      appendCalLog(`Live FPS p5: ${event.live_fps_p5.toFixed(1)} vs Replay p5: ${event.replay_fps_p5.toFixed(1)}`);
      loadCalibrationStatus();
      resetCalUI();
      break;

    case "cal_error":
      appendCalLog(`Error: ${event.msg}`);
      resetCalUI();
      break;

    case "cal_aborted":
      appendCalLog("Calibration cancelled.");
      resetCalUI();
      break;
  }

  if (TERMINAL.includes(event.type) && _calSSE) {
    _calSSE.close();
    _calSSE = null;
  }
}

function appendCalLog(msg) {
  const el = document.getElementById("cal-log");
  if (!el) return;
  el.style.display = "";
  const ts = new Date().toLocaleTimeString("en-US", { hour12: false });
  el.textContent += `[${ts}] ${msg}\n`;
  el.scrollTop = el.scrollHeight;
}

function resetCalUI() {
  const startBtn = document.getElementById("btn-cal-start");
  const stopBtn = document.getElementById("btn-cal-stop");
  if (startBtn) startBtn.style.display = "";
  if (stopBtn) stopBtn.style.display = "none";
}
