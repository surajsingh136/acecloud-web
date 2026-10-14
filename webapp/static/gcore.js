/* GCORE Proxy Maker - dashboard frontend */
"use strict";
const $ = (id) => document.getElementById(id);
let META = { regions: [], images: [], sizes: [] }, ROWS = [], LASTN = 0;
let ACC = {}, ACCLIST = [], CURREGION = "";

/* ---------------- transport (PIN aware) ---------------- */
async function J(url, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  const pin = localStorage.getItem("acepin");
  if (pin) opts.headers["X-Pin"] = pin;
  const r = await fetch(url, opts);
  if (r.status === 401) {
    const p = prompt("Enter website PIN:");
    if (p) { localStorage.setItem("acepin", p); return J(url, opts); }
    throw new Error("no pin");
  }
  return r.json();
}
const POST = (u, o) => J(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(o) });

function toast(m) {
  const t = $("toast"); t.textContent = m; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 2600);
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ---------------- modal ---------------- */
function openModal(title, html) { $("modal-title").textContent = title; $("modal-body").innerHTML = html; $("modal").classList.remove("hidden"); }
function closeModal() { $("modal").classList.add("hidden"); }

/* ---------------- accounts ---------------- */
async function loadAccounts() {
  let d; try { d = await J("/api/gcore/accounts"); } catch (e) { return; }
  ACCLIST = d.accounts || [];
  ACC = {}; ACCLIST.forEach(a => { ACC[a.id] = a; });
  const sel = $("gc_acct");
  sel.innerHTML = ACCLIST.length
    ? ACCLIST.map(a => `<option value="${esc(a.id)}"${a.id === d.active ? " selected" : ""}>${esc(a.label || a.id)}</option>`).join("")
    : '<option value="">(no account)</option>';
}
async function acctChanged() {
  const id = $("gc_acct").value; if (!id) return;
  const d = await POST("/api/gcore/accounts/activate", { id });
  if (d.ok) { toast("Active account switched"); await loadMeta(); await loadServers(); pollInfo(); }
  else toast(d.msg || "Failed");
}
function keyModal() {
  const rows = ACCLIST.map(a => `
    <tr class="${a.id === $("gc_acct").value ? "act" : ""}">
      <td>${a.id === $("gc_acct").value ? '<span class="dot" title="active"></span>' : `<button class="mini" onclick="useAcct('${a.id}')">Use</button>`}</td>
      <td>${esc(a.label)}${a.editable ? "" : ' <span class="ro">(read-only)</span>'}</td>
      <td>${a.has_key ? "set" : "-"}</td>
      <td>${esc(a.project_id || "-")}</td>
      <td>${esc(a.region_id || "-")}</td>
      <td>${a.editable ? `<button class="mini" onclick="delAcct('${a.id}')">Del</button>` : ""}</td>
    </tr>`).join("");
  openModal("Gcore Accounts", `
    <table class="tv">
      <thead><tr><th></th><th>Label</th><th>Key</th><th>Project</th><th>Region</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6">No accounts</td></tr>'}</tbody>
    </table>
    <label class="ro">Label</label>
    <input id="kalabel" class="in" style="width:100%" placeholder="e.g. Main">
    <label class="ro">API key</label>
    <input id="kak" class="in" style="width:100%" placeholder="gcore api key">
    <label class="ro">Project id (optional)</label>
    <input id="kapid" class="in" style="width:100%" placeholder="auto-detect">
    <label class="ro">Region id (optional)</label>
    <input id="karid" class="in" style="width:100%" value="108">
    <label class="ro" style="display:block;margin-top:6px"><input type="checkbox" id="kause" checked> use as active after saving</label>
    <div class="btns">
      <button class="btn sm" onclick="verifyKey()">Verify key</button>
      <button class="btn create sm" onclick="saveAcct()">Save account</button>
    </div>
    <div id="kout" style="margin-top:8px"></div>`);
}
function useAcct(id) { const s = $("gc_acct"); s.value = id; closeModal(); acctChanged(); }
function delAcct(id) { if (!confirm("Delete this account?")) return; POST("/api/gcore/accounts/delete", { id }).then(() => { toast("Deleted"); loadAccounts().then(keyModal); }); }
async function verifyKey() {
  const key = ($("kak").value || "").trim();
  if (!key) { toast("Enter API key"); return; }
  $("kout").textContent = "Checking...";
  let d; try { d = await POST("/api/gcore/verify", { apikey: key }); } catch (e) { $("kout").textContent = "Failed"; return; }
  $("kout").innerHTML = d.ok ? '<span class="ok">OK</span> - ' + esc(d.user || "verified")
                             : '<span class="bad">' + esc(d.msg || "Invalid key") + "</span>";
}
async function saveAcct() {
  const body = {
    label: ($("kalabel").value || "").trim(),
    apikey: ($("kak").value || "").trim(),
    project_id: ($("kapid").value || "").trim(),
    region_id: ($("karid").value || "").trim(),
    use: $("kause").checked,
  };
  if (!body.apikey) { toast("API key required"); return; }
  let d; try { d = await POST("/api/gcore/accounts/add", body); } catch (e) { toast("Failed"); return; }
  toast(d.ok ? "Saved" : (d.msg || "Failed"));
  if (d.ok) { await loadAccounts(); closeModal(); loadMeta(); }
}

/* ---------------- meta / create form ---------------- */
function fillRegions() {
  const regs = META.regions || [];
  $("c_region").innerHTML = regs.length
    ? regs.map(r => `<option value="${esc(r.id)}"${String(r.id) === String(CURREGION) ? " selected" : ""}>${esc(r.name)}${r.zone ? " - " + esc(r.zone) : ""}${r.basic ? "" : " (no basic vm)"}</option>`).join("")
    : '<option value="">(no region)</option>';
}
function fillImages() {
  const imgs = META.images || [];
  $("c_image").innerHTML = imgs.length
    ? imgs.map((im, i) => `<option value="${i}">${esc(im.name)}${im.windows ? " [Win]" : ""}${im.arch === "arm64" ? " [arm]" : ""}</option>`).join("")
    : '<option value="">(no image)</option>';
}
function fillSizes() {
  const sizes = META.sizes || [];
  $("c_type").innerHTML = sizes.length
    ? sizes.map((t, i) => {
        const rate = (t.rate != null) ? " - Rs." + Number(t.rate).toFixed(2) + "/hr" : "";
        return `<option value="${i}">${esc(t.name)} - ${t.cpu}vCPU/${t.ram}GB${rate}</option>`;
      }).join("")
    : '<option value="">(no size)</option>';
}
function isWinImage(im) { return !!(im && im.windows); }
function onImageChange() {
  const im = (META.images || [])[parseInt($("c_image").value || "0", 10)] || {};
  const win = isWinImage(im);
  $("c_user").disabled = win; $("c_pass").disabled = win; $("c_port").disabled = win;
  $("c_testline").disabled = win; $("btn_test").disabled = win;
  $("btn_create").textContent = win ? "Create Server" : "Create Proxies";
}
async function loadMeta(region) {
  const q = region ? ("?region=" + encodeURIComponent(region)) : "";
  let d; try { d = await J("/api/gcore/meta" + q); } catch (e) { return; }
  META = d || {}; META.regions = d.regions || []; META.images = d.images || []; META.sizes = d.sizes || [];
  CURREGION = d.region_id || region || "";
  fillRegions();
  fillImages();
  fillSizes();
  onImageChange();
  if (d.user) $("balance").textContent = "Gcore | " + d.user + (d.project_id ? "  (proj " + d.project_id + ")" : "");
}

async function regionChanged() {
  const rid = $("c_region").value;
  if (!rid || rid === CURREGION) return;
  CURREGION = rid;
  toast("Loading region " + rid + " ...");
  await loadMeta(rid);
}

async function createProxies() {
  const ti = parseInt($("c_type").value || "0", 10);
  const t = (META.sizes || [])[ti] || {};
  const im = (META.images || [])[parseInt($("c_image").value || "0", 10)] || {};
  if (!im.id || !t.name) { toast("Pick an image and size"); return; }
  const body = {
    region_id: $("c_region").value,
    image_id: im.id, flavor: t.name,
    volume_size: $("c_disk").value || 10,
    count: $("c_count").value || 1,
    instance_name: ($("c_name").value || "").trim() || "gcoreproxy",
    proxy_user: ($("c_user").value || "ajay").trim() || "ajay",
    proxy_pass: $("c_pass").value || "ajay",
    proxy_port: parseInt($("c_port").value || "3128", 10),
    root_password: $("c_root").value,
    windows: isWinImage(im),
  };
  let d; try { d = await POST("/api/gcore/create", body); } catch (e) { toast("Failed"); return; }
  toast(d.ok ? "Creating... watch the log" : (d.msg || "Failed"));
}

/* ---------------- servers ---------------- */
function proxyLine(r) {
  if (!r.ip) return "";
  return r.windows ? `${r.ip}:3389:Administrator:${$("c_root").value || ""}` : `${r.ip}:${$("c_port").value || "3128"}:${$("c_user").value || "ajay"}:${$("c_pass").value || "ajay"}`;
}
async function loadServers() {
  let d; try { d = await J("/api/gcore/state"); } catch (e) { return; }
  ROWS = d.rows || [];
  if (!ROWS.length) {
    $("srvBody").innerHTML = '<tr><td colspan="8" class="empty">No instances. Create some above.</td></tr>';
  } else {
    $("srvBody").innerHTML = ROWS.map((r, i) => `
      <tr data-i="${i}">
        <td class="chk"><input type="checkbox" class="rowchk" data-i="${i}"></td>
        <td>${esc(r.name)}</td><td>${esc(r.ip || "-")}</td>
        <td>${esc(r.status)}</td><td>${esc(r.os)}</td><td>${esc(r.size)}</td>
        <td>${esc(proxyLine(r) || "-")}</td><td>${esc(r.created)}</td>
      </tr>`).join("");
  }
  const running = ROWS.filter(r => ["active", "running", "ready"].includes((r.status || "").toLowerCase())).length;
  const prox = ROWS.filter(r => r.ip).length;
  $("stats").textContent = `Instances: ${ROWS.length}   |   Active: ${running}   |   With IP: ${prox}`;
}
function selectedRows() {
  const out = [];
  document.querySelectorAll(".rowchk").forEach(c => { if (c.checked) out.push(ROWS[parseInt(c.dataset.i, 10)]); });
  return out;
}
document.addEventListener("change", (e) => {
  if (e.target.classList && e.target.classList.contains("rowchk")) {
    const tr = e.target.closest("tr"); if (tr) tr.classList.toggle("sel", e.target.checked);
  }
});
function pickRow() { const s = selectedRows(); return s[0] || ROWS[0]; }
function copyText(txt, label) {
  if (!txt) { toast("No value"); return; }
  navigator.clipboard.writeText(txt).then(() => toast(label + ": " + txt), () => toast("Copy failed"));
}
function copyIP() { const r = pickRow(); if (!r) { toast("No instance"); return; } copyText(r.ip, "Copied IP"); }
function copyProxy() { const r = pickRow(); if (!r) { toast("No instance"); return; } copyText(proxyLine(r), "Copied proxy"); }
async function deleteSelected() {
  const sel = selectedRows();
  if (!sel.length) { toast("Select instances first"); return; }
  if (!confirm("Delete " + sel.length + " instance(s)?")) return;
  const d = await POST("/api/gcore/delete", { ids: sel.map(r => r.vm_id) });
  toast(d.ok ? "Deleting... watch the log" : "Failed");
  setTimeout(loadServers, 2500);
}
async function deleteAll() {
  if (!confirm("Delete ALL gcore instances across every region?")) return;
  const d = await POST("/api/gcore/delete", { all: true });
  toast(d.ok ? "Deleting all... watch the log" : "Failed");
  setTimeout(loadServers, 3000);
}

/* ---------------- test proxy ---------------- */
async function testProxy() {
  const line = ($("c_testline").value || "").trim();
  if (!line) { toast("Enter ip:port:user:pass"); return; }
  toast("Testing " + line + " ...");
  let d; try { d = await POST("/api/gcore/testproxy", { line }); } catch (e) { toast("Failed"); return; }
  if (!d.ok) { toast(d.msg || "Failed"); return; }
  (d.results || []).forEach(r => {
    if (r.ok) logLine(`  ${r.name}: HTTP ${r.code} (${r.ms} ms)`, "ok");
    else logLine(`  ${r.name}: FAIL ${r.msg || ""}`, "error");
  });
  toast((d.results || []).every(r => r.ok) ? "Proxy works" : "Proxy test finished (see log)");
}

/* ---------------- open output ---------------- */
async function openOutput() {
  let d; try { d = await J("/api/gcore/output"); } catch (e) { return; }
  openModal("Output - " + d.file, d.text
    ? `<div class="mono" style="font:12px/1.5 Consolas,monospace;white-space:pre-wrap">${esc(d.text)}</div>
       <div style="margin-top:10px"><button class="btn sm" onclick="copyText(document.querySelector('.mono').textContent,'Copied')">Copy all</button></div>`
    : `<div class="bad">${esc(d.file)} is empty.</div>`);
}

/* ---------------- logs / info ---------------- */
function logLine(m, cls) {
  const box = $("log");
  box.innerHTML += `<div class="${cls || ""}">${esc(m)}</div>`;
  box.scrollTop = box.scrollHeight;
}
async function pollLogs() {
  try {
    const d = await J("/api/logs?since=" + LASTN);
    if (d.logs && d.logs.length) {
      d.logs.forEach(l => {
        const cls = l.k === "error" ? "e" : l.k === "ok" ? "o" : l.k === "warn" ? "w" : l.k === "sys" ? "s" : "";
        logLine("[" + l.t + "] " + l.m, cls);
      });
      LASTN = d.n;
    }
  } catch (e) { /* ignore */ }
  setTimeout(pollLogs, 1500);
}
async function pollInfo() {
  try {
    const d = await J("/api/gcore/meta");
    if (d && d.user) $("balance").textContent = "Gcore | " + d.user + (d.project_id ? "  (proj " + d.project_id + ")" : "");
  } catch (e) { /* ignore */ }
  setTimeout(pollInfo, 30000);
}

/* ---------------- init ---------------- */
function wire() {
  $("c_image").addEventListener("change", onImageChange);
  $("c_region").addEventListener("change", regionChanged);
  $("gc_acct").addEventListener("change", acctChanged);
  $("btn_keys").addEventListener("click", keyModal);
  $("modal-x").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  $("btn_create").addEventListener("click", createProxies);
  $("btn_delall").addEventListener("click", deleteAll);
  $("btn_test").addEventListener("click", testProxy);
  $("btn_open").addEventListener("click", openOutput);
  $("btn_refresh").addEventListener("click", loadServers);
  $("btn_copyip").addEventListener("click", copyIP);
  $("btn_copyproxy").addEventListener("click", copyProxy);
  $("btn_delsel").addEventListener("click", deleteSelected);
}

(async function () {
  wire();
  await loadAccounts();
  await loadMeta();
  await loadServers();
  pollLogs();
  pollInfo();
})();
