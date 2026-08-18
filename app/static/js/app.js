(() => {
  "use strict";

  const STORAGE_KEY = "m4300_switches_v1";
  let switches = loadSwitches();
  let modules = []; // populated from /api/modules
  let selectedModuleIds = new Set();
  let selectedSwitchIds = new Set();

  // ---------- persistence ----------

  function loadSwitches() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return parsed.map((s) => ({ status: "pending", statusMsg: "", ...s }));
    } catch {
      return [];
    }
  }

  function saveSwitches() {
    const toSave = switches.map(({ status, statusMsg, ...rest }) => rest);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(toSave));
  }

  function newSwitch() {
    return {
      id: crypto.randomUUID(),
      name: "",
      host: "",
      port: 8443,
      username: "",
      password: "",
      verify_tls: false,
      model: "",
      firmware: "",
      status: "pending",
      statusMsg: "",
    };
  }

  // ---------- tabs ----------

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(`panel-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "explorer") renderExploreSwitchOptions();
      if (btn.dataset.tab === "builder") {
        renderSwitchPicker();
        renderModuleGrid();
      }
    });
  });

  // ---------- toast ----------

  let toastTimer = null;
  function toast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  // ---------- switches tab ----------

  function renderSwitchTable() {
    const tbody = document.getElementById("switch-tbody");
    tbody.innerHTML = "";
    if (switches.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="9" class="empty-state">No switches yet. Click "Add Switch" to get started.</td>`;
      tbody.appendChild(tr);
      return;
    }
    switches.forEach((sw) => {
      const tr = document.createElement("tr");
      tr.dataset.id = sw.id;
      tr.innerHTML = `
        <td><input type="text" data-field="name" value="${escapeAttr(sw.name)}" placeholder="IDF2"></td>
        <td><input type="text" data-field="host" value="${escapeAttr(sw.host)}" placeholder="10.1.1.5"></td>
        <td><input type="number" data-field="port" value="${sw.port}"></td>
        <td><input type="text" data-field="username" value="${escapeAttr(sw.username)}" placeholder="admin"></td>
        <td><input type="password" data-field="password" value="${escapeAttr(sw.password)}"></td>
        <td style="text-align:center"><input type="checkbox" data-field="verify_tls" ${sw.verify_tls ? "checked" : ""}></td>
        <td class="model-cell">${modelCell(sw)}</td>
        <td>${statusPill(sw)}</td>
        <td style="display:flex; gap:0.35rem;">
          <button class="btn secondary test-btn" style="padding:0.3rem 0.5rem;">Test</button>
          <button class="btn danger remove-btn" style="padding:0.3rem 0.5rem;">&times;</button>
        </td>
      `;
      tbody.appendChild(tr);

      tr.querySelectorAll("input").forEach((input) => {
        input.addEventListener("input", () => {
          const field = input.dataset.field;
          sw[field] = field === "verify_tls" ? input.checked : field === "port" ? Number(input.value) : input.value;
          saveSwitches();
        });
      });
      tr.querySelector(".test-btn").addEventListener("click", () => testConnection(sw, tr));
      tr.querySelector(".remove-btn").addEventListener("click", () => {
        switches = switches.filter((s) => s.id !== sw.id);
        selectedSwitchIds.delete(sw.id);
        saveSwitches();
        renderSwitchTable();
      });
    });
  }

  function statusPill(sw) {
    if (sw.status === "ok") return `<span class="status-pill ok">&#10003; Connected</span>`;
    if (sw.status === "fail") return `<span class="status-pill fail" title="${escapeAttr(sw.statusMsg)}">&#10007; Failed</span>`;
    if (sw.status === "testing") return `<span class="status-pill pending"><span class="spinner" style="border-top-color:#888;border-color:rgba(0,0,0,0.15)"></span> Testing...</span>`;
    return `<span class="status-pill pending">Not tested</span>`;
  }

  function modelCell(sw) {
    if (!sw.model) return `<span class="empty-state" style="padding:0">Run Test &rarr;</span>`;
    return `<div style="font-weight:600">${escapeHtml(sw.model)}</div><div style="color:var(--gray-muted);font-size:0.75rem">${escapeHtml(sw.firmware || "")}</div>`;
  }

  async function testConnection(sw, tr) {
    sw.status = "testing";
    tr.querySelector("td:nth-child(8)").innerHTML = statusPill(sw);
    try {
      const resp = await fetch("/api/test-connection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ switch: toSwitchPayload(sw) }),
      });
      const data = await resp.json();
      sw.status = data.success ? "ok" : "fail";
      sw.statusMsg = data.success ? "Connected" : data.message;
      if (data.success) {
        sw.model = data.model || sw.model;
        sw.firmware = data.firmware || sw.firmware;
      }
    } catch (err) {
      sw.status = "fail";
      sw.statusMsg = String(err);
    }
    saveSwitches();
    tr.querySelector(".model-cell").innerHTML = modelCell(sw);
    tr.querySelector("td:nth-child(8)").innerHTML = statusPill(sw);
  }

  function toSwitchPayload(sw) {
    return {
      name: sw.name || sw.host,
      host: sw.host,
      port: Number(sw.port) || 8443,
      username: sw.username,
      password: sw.password,
      verify_tls: !!sw.verify_tls,
    };
  }

  document.getElementById("add-switch-btn").addEventListener("click", () => {
    switches.push(newSwitch());
    saveSwitches();
    renderSwitchTable();
  });

  // ---------- explorer tab ----------

  function renderExploreSwitchOptions() {
    const sel = document.getElementById("explore-switch");
    sel.innerHTML = switches
      .filter((s) => s.host)
      .map((s) => {
        const label = `${s.name || s.host} (${s.host}${s.model ? " · " + s.model : ""})`;
        return `<option value="${s.id}">${escapeHtml(label)}</option>`;
      })
      .join("") || `<option value="">No switches saved yet</option>`;
  }

  function renderExploreModuleOptions() {
    const sel = document.getElementById("explore-module");
    sel.innerHTML = modules.map((m) => `<option value="${m.id}">${escapeHtml(m.label)}</option>`).join("");
  }

  document.getElementById("explore-fetch-btn").addEventListener("click", async () => {
    const swId = document.getElementById("explore-switch").value;
    const moduleId = document.getElementById("explore-module").value;
    const sw = switches.find((s) => s.id === swId);
    const resultEl = document.getElementById("explore-result");
    if (!sw) {
      toast("Add and select a switch first.");
      return;
    }
    resultEl.innerHTML = `<div class="empty-state"><span class="spinner" style="border-top-color:#001E62;border-color:rgba(0,0,0,0.15)"></span> Fetching ${moduleId}...</div>`;
    try {
      const resp = await fetch("/api/explore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ switch: toSwitchPayload(sw), module: moduleId }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        resultEl.innerHTML = `<div class="toast show" style="position:static;background:#FDECEC;color:#B02A2A;">${escapeHtml(data.detail || "Request failed")}</div>`;
        return;
      }
      resultEl.innerHTML = `<pre class="result-json">${escapeHtml(JSON.stringify(data.data, null, 2))}</pre>`;
    } catch (err) {
      resultEl.innerHTML = `<div class="toast show" style="position:static;background:#FDECEC;color:#B02A2A;">${escapeHtml(String(err))}</div>`;
    }
  });

  // ---------- report builder tab ----------

  function renderSwitchPicker() {
    const el = document.getElementById("rb-switch-picker");
    const usable = switches.filter((s) => s.host);
    if (usable.length === 0) {
      el.innerHTML = `<div class="empty-state">No switches saved yet. Go to the Switches tab first.</div>`;
      return;
    }
    if (selectedSwitchIds.size === 0) usable.forEach((s) => selectedSwitchIds.add(s.id));
    el.innerHTML = usable
      .map(
        (s) => `<div class="switch-chip ${selectedSwitchIds.has(s.id) ? "selected" : ""}" data-id="${s.id}">
          ${escapeHtml(s.name || s.host)}${s.model ? ` <span style="opacity:0.7">· ${escapeHtml(s.model)}</span>` : ""}
        </div>`
      )
      .join("");
    el.querySelectorAll(".switch-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const id = chip.dataset.id;
        if (selectedSwitchIds.has(id)) selectedSwitchIds.delete(id);
        else selectedSwitchIds.add(id);
        chip.classList.toggle("selected");
      });
    });
  }

  function renderModuleGrid() {
    const el = document.getElementById("rb-module-grid");
    if (modules.length === 0) return;
    if (selectedModuleIds.size === 0) {
      modules.filter((m) => m.default_in_report).forEach((m) => selectedModuleIds.add(m.id));
    }
    el.innerHTML = modules
      .map(
        (m) => `<label class="module-check">
          <input type="checkbox" data-id="${m.id}" ${selectedModuleIds.has(m.id) ? "checked" : ""}>
          <div>
            <div class="cat">${escapeHtml(m.category)}</div>
            <div class="name">${escapeHtml(m.label)}</div>
            <div class="desc">${escapeHtml(m.description)}</div>
          </div>
        </label>`
      )
      .join("");
    el.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) selectedModuleIds.add(cb.dataset.id);
        else selectedModuleIds.delete(cb.dataset.id);
      });
    });
  }

  document.getElementById("rb-generate-btn").addEventListener("click", async () => {
    const siteName = document.getElementById("rb-site-name").value.trim();
    const statusEl = document.getElementById("rb-status");
    if (!siteName) {
      toast("Site name is required.");
      return;
    }
    const chosenSwitches = switches.filter((s) => selectedSwitchIds.has(s.id)).map(toSwitchPayload);
    if (chosenSwitches.length === 0) {
      toast("Select at least one switch.");
      return;
    }
    if (selectedModuleIds.size === 0) {
      toast("Select at least one data module.");
      return;
    }

    const btn = document.getElementById("rb-generate-btn");
    btn.disabled = true;
    statusEl.textContent = `Fetching live data from ${chosenSwitches.length} switch(es) and building PDF...`;

    try {
      const resp = await fetch("/api/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          site_name: siteName,
          client_name: document.getElementById("rb-client-name").value.trim(),
          prepared_by: document.getElementById("rb-prepared-by").value.trim(),
          notes: document.getElementById("rb-notes").value.trim(),
          switches: chosenSwitches,
          modules: Array.from(selectedModuleIds),
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `Request failed (${resp.status})`);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${siteName.replace(/\s+/g, "-")}-switch-report.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      statusEl.textContent = "Report downloaded.";
      toast("PDF report generated.");
    } catch (err) {
      statusEl.textContent = "";
      toast(`Failed: ${err.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  // ---------- helpers ----------

  function escapeHtml(str) {
    return String(str ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function escapeAttr(str) {
    return escapeHtml(str);
  }

  // ---------- init ----------

  async function init() {
    renderSwitchTable();
    try {
      const resp = await fetch("/api/modules");
      modules = await resp.json();
    } catch {
      modules = [];
    }
    renderExploreModuleOptions();
    renderExploreSwitchOptions();
    renderSwitchPicker();
    renderModuleGrid();
  }

  init();
})();
