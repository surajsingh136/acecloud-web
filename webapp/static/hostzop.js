/* HOSTZOP Proxy Maker - dashboard frontend (mirrors the desktop GUI) */
"use strict";
const $ = (id) => document.getElementById(id);
let META = { sizes: [], images: [] }, ROWS = [], SNAPS = [], LASTN = 0;
let ACC = {}, ACCLIST = [];

/* ---------------- transport (PIN aware) ---------------- */
async function J(url, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  const pin = localStorage.getItem("acepin");
  if (pin) opts.headers["X-Pin"] = pin;
  const r = await fetch(url, opts);
  if (r.status === 401) {
    window.location.href = "/login";
    throw new Error("login required");
  }
  return r.json();
}
const POST = (u, o) => J(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(o) });

function toast(m) {
  const t = $("toast"); t.textContent = m; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 2400);
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function osKey(v) { const s = String(v || "").toLowerCase(); return s.startsWith("win") ? "windows" : s.startsWith("cent") ? "centos" : "ubuntu"; }

/* ---------------- busy lock: while a task runs, lock the buttons (same as AceCloud) ---------------- */
const BUSY_BTNS = ["btn_create", "btn_delall", "btn_snap", "btn_test", "btn_delsel"];
let _busyTimer = null;
function setBusy(on) { BUSY_BTNS.forEach(id => { const e = $(id); if (e) e.disabled = !!on; }); }
function watchBusy() {
  setBusy(true);
  if (_busyTimer) return;
  _busyTimer = setInterval(async () => {
    let d; try { d = await J("/api/hostzop/state"); } catch (e) { return; }
    if (!d.busy) { clearInterval(_busyTimer); _busyTimer = null; setBusy(false); try { await loadServers(); } catch (e) {} }
  }, 2500);
}

/* ---------------- modal ---------------- */
function openModal(title, html) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = html;
  $("modal").classList.remove("hidden");
}
function closeModal() { $("modal").classList.add("hidden"); }

/* ---------------- accounts ---------------- */
async function loadAccounts() {
  let d;
  try { d = await J("/api/hostzop/accounts"); } catch (e) { return; }
  ACCLIST = d.accounts || [];
  ACC = {}; ACCLIST.forEach(a => { ACC[a.id] = a; });
  const sel = $("hz_acct");
  sel.innerHTML = ACCLIST.length
    ? ACCLIST.map(a => `<option value="${esc(a.id)}"${a.id === d.active ? " selected" : ""}>${esc(a.label || a.id)}${a.location ? " @" + esc(a.location) : ""}</option>`).join("")
    : '<option value="">(no account)</option>';
}
async function acctChanged() {
  const id = $("hz_acct").value; if (!id) return;
  const d = await POST("/api/hostzop/accounts/activate", { id });
  if (d.ok) { toast("Active account switched"); await loadMeta(); await loadServers(); pollBalance(); }
  else toast(d.msg || "Failed");
}

function keyModal() {
  const rows = ACCLIST.map(a => `
    <tr class="${a.editable ? "" : "act"}">
      <td>${a.id === $("hz_acct").value ? '<span class="dot" title="active"></span>' : `<button class="mini" onclick="useAcct('${a.id}')">Use</button>`}</td>
      <td>${esc(a.label)}${a.editable ? "" : ' <span class="ro">(read-only)</span>'}</td>
      <td>${a.has_key ? "set" : "-"}</td>
      <td>${esc(a.billing_account_id || "-")}</td>
      <td>${esc(a.location || "-")}</td>
      <td>${a.editable ? `<button class="mini" onclick="editAcct('${a.id}')">Edit</button>
        <button class="mini del" onclick="delAcct('${a.id}')">Del</button>` : ""}</td>
    </tr>`).join("");
  openModal("Hostzop accounts", `
    <table class="acct"><thead><tr><th>Use</th><th>Label</th><th>Key</th><th>Billing</th><th>Location</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="6">No accounts</td></tr>'}</tbody></table>
    <hr style="border:0;border-top:1px solid #BFD0DE;margin:14px 0">
    <div id="kform">
      <label>API key</label><input id="kak" class="in" placeholder="hostzop API key">
      <label>Label</label><input id="kalabel" class="in" placeholder="e.g. Main">
      <label>Billing account id (optional)</label><input id="kabill" class="in">
      <label>Location (optional)</label><select id="kaloc" class="in"><option value="">(default)</option></select>
      <label><input type="checkbox" id="kause" checked> use as active after saving</label>
      <div style="margin-top:10px;display:flex;gap:8px">
        <button class="btn sm" onclick="verifyKey()">Verify key</button>
        <button class="btn create sm" onclick="saveAcct()">Save account</button>
      </div>
      <div id="kout" style="margin-top:8px"></div>
    </div>`);
}
function useAcct(id) { const s = $("hz_acct"); s.value = id; closeModal(); acctChanged(); }
function delAcct(id) { if (!confirm("Delete this account?")) return; POST("/api/hostzop/accounts/delete", { id }).then(d => { toast(d.ok ? "Deleted" : (d.msg || "Failed")); loadAccounts().then(keyModal); }); }
function editAcct(id) {
  const a = ACC[id] || {};
  $("kak").value = a.apikey || ""; $("kalabel").value = a.label || "";
  $("kabill").value = a.billing_account_id || ""; $("kause").checked = true;
  $("kform").dataset.id = id;
  fillLocations([], a.location || "");
  $("kak").scrollIntoView({ behavior: "smooth" });
}
function fillLocations(locs, sel) {
  const s = $("kaloc"); if (!s) return;
  s.innerHTML = '<option value="">(default)</option>' +
    (locs || []).map(l => `<option value="${esc(l.slug)}">${esc(l.name)}${l.is_default ? " (default)" : ""}</option>`).join("");
  if (sel) s.value = sel;
}
async function verifyKey() {
  const key = ($("kak").value || "").trim();
  if (!key) { toast("Enter API key first"); return; }
  $("kout").textContent = "Verifying...";
  let d; try { d = await POST("/api/hostzop/verify", { apikey: key }); } catch (e) { $("kout").textContent = "Failed"; return; }
  if (d.ok) {
    $("kout").innerHTML = '<span class="ok">OK</span> - ' + esc(d.user || d.email || "verified") +
      " | locations: " + (d.locations || []).length;
    fillLocations(d.locations, $("kaloc").value);
  } else $("kout").innerHTML = '<span class="bad">' + esc(d.msg || "Invalid key") + "</span>";
}
async function saveAcct() {
  const body = {
    id: ($("kform").dataset.id || ""), label: ($("kalabel").value || "").trim(),
    apikey: ($("kak").value || "").trim(), billing_account_id: ($("kabill").value || "").trim(),
    location: $("kaloc").value, use: $("kause").checked,
  };
  if (!body.apikey) { toast("API key required"); return; }
  let d; try { d = await POST("/api/hostzop/accounts/add", body); } catch (e) { toast("Failed"); return; }
  if (d.ok) { toast("Saved"); await loadAccounts(); keyModal(); loadMeta(); }
  else toast(d.msg || "Failed");
}

/* ---------------- meta / create form ---------------- */
function ubuntuVersions() {
  const imgs = META.images || [];
  const u = (imgs.find(x => (x.name || "").toLowerCase() === "ubuntu") || {}).versions || [];
  return u.length ? u : ["22.04", "20.04", "24.04"];
}
function centosVersions() {
  const imgs = META.images || [];
  const c = (imgs.find(x => (x.name || "").toLowerCase() === "centos") || {}).versions || [];
  return c.length ? c : ["9.stream", "8.stream"];
}
function sizeLabel(s, win) {
  const v = win ? (s.rate_win != null ? s.rate_win : s.rate) : s.rate;
  const rate = (v != null) ? " - Rs." + Number(v).toFixed(2) + "/hr" : "";
  return `${s.name} - ${s.cpu} vCPU/${s.ram / 1024}GB/${s.disk}GB${rate}${win ? " [WinLic]" : ""}`;
}
function fillSizes(win) {
  const sizes = META.sizes || [];
  $("c_size").innerHTML = sizes.map((s, i) => `<option value="${i}">${esc(sizeLabel(s, win))}</option>`).join("");
}
async function refreshSizes() {
  const pool = $("c_pool") ? $("c_pool").value : "";
  const sel = $("c_size") ? $("c_size").value : "0";
  let d; try { d = await J("/api/hostzop/meta" + (pool ? "?pool=" + encodeURIComponent(pool) : "")); } catch (e) { return; }
  if (d && d.sizes) META.sizes = d.sizes;
  fillSizes($("c_os").value === "Windows Server");
  if ($("c_size") && $("c_size").options.length) $("c_size").value = sel;
}
function onOsChange() {
  const os = $("c_os").value;
  let vers, def;
  if (os === "Windows Server") { vers = ["2019", "2022", "2025"]; def = "2022"; }
  else if (os === "CentOS") { vers = centosVersions(); def = vers[vers.length - 1]; }
  else { vers = ubuntuVersions(); def = vers.includes("22.04") ? "22.04" : vers[0]; }
  $("c_ver").innerHTML = vers.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  if (vers.includes(def)) $("c_ver").value = def;

  const win = os === "Windows Server";
  $("c_user").disabled = win; $("c_pass").disabled = win; $("c_testline").disabled = win;
  $("btn_test").disabled = win;
  $("btn_create").textContent = win ? "Create Server" : "Create Proxies";
  fillSizes(win);
  if (win && !$("c_name").value.startsWith("WIN")) $("c_name").value = "WIN01";
  else if (!win && $("c_name").value === "WIN01") $("c_name").value = "hzproxy";
}
async function loadMeta() {
  let d; try { d = await J("/api/hostzop/meta"); } catch (e) { return; }
  META = d || {}; META.sizes = d.sizes || []; META.images = d.images || [];
  const pools = d.pools || [];
  $("c_pool").innerHTML = pools.length
    ? pools.map(p => `<option value="${esc(p.uuid)}"${p.default ? " selected" : ""}>${esc(p.name)}${p.default ? " (default)" : ""}</option>`).join("")
    : '<option value="">(account default)</option>';
  if (d.billing_account_id) $("c_bill").value = d.billing_account_id;
  onOsChange();
  refreshSizes();
  loadTemplates();
}
function osVersionsItems() { return []; }

async function loadTemplates() {
  let d; try { d = await J("/api/hostzop/snaps"); } catch (e) { return; }
  SNAPS = d.snaps || [];
  const items = ['<option value="">None (fresh install)</option>'].concat(
    SNAPS.map((s, i) => `<option value="${i}">${esc(s.name)}  |  ${esc(s.vm_name || "")}  |  ${esc(s.date || "")}</option>`));
  $("c_tpl").innerHTML = items.join("");
}

async function createServers() {
  const si = parseInt($("c_size").value || "0", 10);
  const sz = (META.sizes || [])[si] || {};
  const osName = $("c_os").value;
  const tplIdx = $("c_tpl").value;
  let template = null;
  if (tplIdx !== "" && SNAPS[tplIdx]) {
    const s = SNAPS[tplIdx];
    template = { vm_uuid: s.vm_uuid, replica_uuid: s.replica_uuid, username: osKey(osName) === "windows" ? "Administrator" : "root" };
  }
  const body = {
    os_name: osKey(osName), os_version: $("c_ver").value,
    vcpu: sz.cpu, ram: sz.ram, disks: sz.disk,
    count: $("c_count").value, instance_name: ($("c_name").value || "").trim() || "hzproxy",
    network_name: ($("c_net").value || "My Network").trim() || "My Network",
    proxy_user: ($("c_user").value || "ajay").trim() || "ajay", proxy_pass: $("c_pass").value || "ajay",
    proxy_port: 3128, root_password: $("c_root").value,
    billing_account_id: ($("c_bill").value || "").trim(),
    designated_pool_uuid: $("c_pool").value, reserve_public_ip: true,
  };
  if (template) body.template = template;
  body.hours = parseInt(($("c_hours") && $("c_hours").value) || "3", 10);
  let d; try { d = await POST("/api/hostzop/create", body); } catch (e) { toast("Failed"); return; }
  if (d.ok) { toast("Creating... watch the log"); watchBusy(); }
  else toast(d.msg || "Failed");
}

/* ---------------- servers ---------------- */
function proxyLine(r) {
  if (!r.ip) return "";
  if (r.windows) return `${r.ip}:3389:Administrator:${r.root_password || $("c_root").value || ""}`;
  return `${r.ip}:${r.proxy_port || 3128}:${r.proxy_user || $("c_user").value || "ajay"}:${r.proxy_pass || $("c_pass").value || "ajay"}`;
}
async function loadServers() {
  let d; try { d = await J("/api/hostzop/state"); } catch (e) { return; }
  setBusy(!!d.busy);
  ROWS = d.rows || [];
  if (!ROWS.length) {
    $("srvBody").innerHTML = '<tr><td colspan="9" class="empty">No servers. Create some above.</td></tr>';
  } else {
    $("srvBody").innerHTML = ROWS.map((r, i) => `
      <tr data-i="${i}">
        <td class="chk"><input type="checkbox" class="rowchk" data-i="${i}"></td>
        <td>${esc(r.name)}</td>
        <td class="cd" data-dl="${r.deadline || 0}">${fmtCd((r.deadline || 0) * 1000 - Date.now())}</td>
        <td class="cp" title="click to copy IP" onclick="cpTxt('${esc(r.ip || "")}')">${esc(r.ip || "-")}</td>
        <td>${esc(r.status)}</td><td>${esc(r.os)}</td><td>${esc(r.size)}</td>
        <td class="cp" title="click to copy proxy" onclick="cpTxt('${esc(proxyLine(r) || "")}')">${esc(proxyLine(r) || "-")}</td><td>${esc(r.created)}</td>
      </tr>`).join("");
  }
  const running = ROWS.filter(r => ["running", "active", "ready"].includes((r.status || "").toLowerCase())).length;
  const prox = ROWS.filter(r => r.ip).length;
  $("stats").textContent = `Servers: ${ROWS.length}   |   Running: ${running}   |   Proxies active: ${prox}`;
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
  if (!txt) { toast("Nothing to copy"); return; }
  navigator.clipboard.writeText(txt).then(() => toast(label + ": " + txt), () => toast("Copy failed"));
}
function cpTxt(txt) { if (txt) copyText(txt, "Copied"); }
function copyIP() { const r = pickRow(); if (!r) { toast("No server"); return; } copyText(r.ip, "Copied IP"); }
function copyProxy() { const r = pickRow(); if (!r) { toast("No server"); return; } copyText(proxyLine(r), "Copied proxy"); }
/* live countdown: decreasing time left until auto-delete */
function fmtCd(ms) {
  if (!ms || ms <= 0) return "-";
  let s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); const ss = s % 60;
  return `${h}h ${String(m).padStart(2, "0")}m ${String(ss).padStart(2, "0")}s`;
}
setInterval(() => {
  document.querySelectorAll(".cd").forEach(el => {
    const dl = parseFloat(el.dataset.dl) || 0;
    el.textContent = fmtCd(dl * 1000 - Date.now());
  });
}, 1000);
function copyRdpPass() {
  const r = pickRow(); if (!r) { toast("No server"); return; }
  const pw = r.root_password || ($("c_root") && $("c_root").value) || "";
  if (!pw) { toast("No RDP password"); return; }
  copyText(pw, "Copied RDP password");
}
async function deleteSelected() {
  const sel = selectedRows();
  if (!sel.length) { toast("Select servers first"); return; }
  if (!confirm("Delete " + sel.length + " server(s)? Bill stops for them.")) return;
  const d = await POST("/api/hostzop/delete", { uuids: sel.map(r => r.uuid) });
  toast(d.ok ? "Deleting... watch the log" : "Failed");
  if (d.ok) watchBusy();
}
async function deleteAll() {
  if (!confirm("Delete ALL hostzop servers? Bill will become 0.")) return;
  const d = await POST("/api/hostzop/delete", { all: true });
  toast(d.ok ? "Deleting all... watch the log" : "Failed");
  if (d.ok) watchBusy();
}

/* ---------------- snapshot ---------------- */
function snapModal() {
  if (!ROWS.length) { toast("No VMs found. Create one first."); return; }
  const opts = ROWS.map((r, i) => `<option value="${i}">${esc(r.name)} | ${esc(r.os)} | ${esc(r.status)}</option>`).join("");
  openModal("Take Snapshot", `
    <label>Instance</label><select id="snap_vm" class="in">${opts}</select>
    <label>Snapshot name</label><input id="snap_name" class="in">
    <div style="margin-top:10px"><button class="btn create sm" onclick="takeSnap()">Take Snapshot</button></div>`);
}
async function takeSnap() {
  const r = ROWS[parseInt($("snap_vm").value || "0", 10)]; if (!r) return;
  const name = ($("snap_name").value || "").trim();
  closeModal();
  const d = await POST("/api/hostzop/snapshot", { vm_uuid: r.uuid, vm_name: r.name, name });
  toast(d.ok ? "Snapshot started... watch the log" : (d.msg || "Failed"));
  if (d.ok) watchBusy();
  setTimeout(loadTemplates, 4000);
}

/* ---------------- test proxy ---------------- */
async function testProxy() {
  const line = ($("c_testline").value || "").trim();
  if (!line) { toast("Enter ip:port:user:pass"); return; }
  toast("Testing " + line + " ...");
  let d; try { d = await POST("/api/hostzop/testproxy", { line }); } catch (e) { toast("Failed"); return; }
  if (!d.ok) { toast(d.msg || "Failed"); return; }
  (d.results || []).forEach(r => {
    if (r.ok) logLine(`  ${r.name}: HTTP ${r.code} (${r.ms} ms)`, "ok");
    else logLine(`  ${r.name}: FAIL ${r.msg || ""}`, "error");
  });
  const okAll = (d.results || []).every(r => r.ok);
  toast(okAll ? "Proxy works" : "Proxy test finished (see log)");
}

/* ---------------- open output ---------------- */
async function openOutput() {
  const which = $("c_os").value === "Windows Server" ? "windows" : "proxy";
  let d; try { d = await J("/api/hostzop/output?file=" + which); } catch (e) { return; }
  openModal("Output - " + d.file, d.text
    ? `<div class="mono">${esc(d.text)}</div><div style="margin-top:10px"><button class="btn sm" onclick="copyText(document.querySelector('.mono').textContent,'Copied')">Copy all</button></div>`
    : `<div class="bad">${esc(d.file)} is empty.</div>`);
}

/* ---------------- logs / balance ---------------- */
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
async function pollBalance() {
  try {
    const d = await J("/api/hostzop/balance");
    if (d.ongoing == null && d.credit == null) { $("balance").textContent = "Balance: n/a"; return; }
    const parts = [];
    const bal = d.ongoing != null ? d.ongoing : d.credit;
    parts.push("Balance: Rs." + Number(bal).toFixed(2));
    if (d.used != null) parts.push("Used: Rs." + Number(d.used).toFixed(2));
    if (d.credit != null) parts.push("Topup: Rs." + Number(d.credit).toFixed(2));
    if (d.unpaid != null) parts.push("Unpaid: Rs." + Number(d.unpaid).toFixed(2));
    $("balance").textContent = parts.join("  |  ");
  } catch (e) { /* ignore */ }
  setTimeout(pollBalance, 20000);
}

/* ---------------- init ---------------- */
function wire() {
  $("c_os").addEventListener("change", onOsChange);
  $("c_pool").addEventListener("change", refreshSizes);
  $("c_tpl").addEventListener("change", () => { /* template selected */ });
  $("hz_acct").addEventListener("change", acctChanged);
  $("btn_keys").addEventListener("click", keyModal);
  $("modal-x").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  $("btn_create").addEventListener("click", createServers);
  $("btn_delall").addEventListener("click", deleteAll);
  $("btn_snap").addEventListener("click", snapModal);
  $("btn_test").addEventListener("click", testProxy);
  $("btn_open").addEventListener("click", openOutput);
  $("btn_refresh").addEventListener("click", loadServers);
  $("btn_copyip").addEventListener("click", copyIP);
  $("btn_copyproxy").addEventListener("click", copyProxy);
  $("btn_copyrdp").addEventListener("click", copyRdpPass);
  $("btn_delsel").addEventListener("click", deleteSelected);
}

(async function () {
  wire();
  await loadAccounts();
  await loadMeta();
  await loadServers();
  pollLogs();
  pollBalance();
})();
