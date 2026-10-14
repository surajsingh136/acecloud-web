/* EXCLOUD Proxy Maker - dashboard frontend (mirrors the desktop GUI) */
"use strict";
const $ = (id) => document.getElementById(id);
let META = { images: [], types: [], subnets: [] }, ROWS = [], SNAPS = [], LASTN = 0;
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
  try { d = await J("/api/excloud/accounts"); } catch (e) { return; }
  ACCLIST = d.accounts || [];
  ACC = {}; ACCLIST.forEach(a => { ACC[a.id] = a; });
  const sel = $("ex_acct");
  sel.innerHTML = ACCLIST.length
    ? ACCLIST.map(a => `<option value="${esc(a.id)}"${a.id === d.active ? " selected" : ""}>${esc(a.label || a.id)}</option>`).join("")
    : '<option value="">(no account)</option>';
}
async function acctChanged() {
  const id = $("ex_acct").value; if (!id) return;
  const d = await POST("/api/excloud/accounts/activate", { id });
  if (d.ok) { toast("Active account switched"); await loadMeta(); await loadServers(); pollInfo(); }
  else toast(d.msg || "Failed");
}

function keyModal() {
  const rows = ACCLIST.map(a => `
    <tr class="${a.editable ? "" : "act"}">
      <td>${a.id === $("ex_acct").value ? '<span class="dot" title="active"></span>' : `<button class="mini" onclick="useAcct('${a.id}')">Use</button>`}</td>
      <td>${esc(a.label)}${a.editable ? "" : ' <span class="ro">(read-only)</span>'}</td>
      <td>${a.has_key ? "set" : "-"}</td>
      <td>${esc(a.zone_id || "-")}</td>
      <td>${esc(a.subnet_id || "-")}</td>
      <td>${a.editable ? `<button class="mini" onclick="editAcct('${a.id}')">Edit</button>
        <button class="mini del" onclick="delAcct('${a.id}')">Del</button>` : ""}</td>
    </tr>`).join("");
  openModal("ExCloud accounts", `
    <table class="acct"><thead><tr><th>Use</th><th>Label</th><th>Key</th><th>Zone</th><th>Subnet</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="6">No accounts</td></tr>'}</tbody></table>
    <hr style="border:0;border-top:1px solid #BFD0DE;margin:14px 0">
    <div id="kform">
      <label>API key</label><input id="kak" class="in" placeholder="sk-excloud-...">
      <label>Label</label><input id="kalabel" class="in" placeholder="e.g. Main">
      <label>Zone id (optional)</label><input id="kazone" class="in">
      <label>Subnet id (optional)</label><input id="kasub" class="in">
      <label><input type="checkbox" id="kause" checked> use as active after saving</label>
      <div style="margin-top:10px;display:flex;gap:8px">
        <button class="btn sm" onclick="verifyKey()">Verify key</button>
        <button class="btn create sm" onclick="saveAcct()">Save account</button>
      </div>
      <div id="kout" style="margin-top:8px"></div>
    </div>`);
}
function useAcct(id) { const s = $("ex_acct"); s.value = id; closeModal(); acctChanged(); }
function delAcct(id) { if (!confirm("Delete this account?")) return; POST("/api/excloud/accounts/delete", { id }).then(d => { toast(d.ok ? "Deleted" : (d.msg || "Failed")); loadAccounts().then(keyModal); }); }
function editAcct(id) {
  const a = ACC[id] || {};
  $("kak").value = a.apikey || ""; $("kalabel").value = a.label || "";
  $("kazone").value = a.zone_id || ""; $("kasub").value = a.subnet_id || ""; $("kause").checked = true;
  $("kform").dataset.id = id;
  $("kak").scrollIntoView({ behavior: "smooth" });
}
async function verifyKey() {
  const key = ($("kak").value || "").trim();
  if (!key) { toast("Enter API key first"); return; }
  $("kout").textContent = "Verifying...";
  let d; try { d = await POST("/api/excloud/verify", { apikey: key }); } catch (e) { $("kout").textContent = "Failed"; return; }
  if (d.ok) $("kout").innerHTML = '<span class="ok">OK</span> - ' + esc(d.org || d.user || d.email || "verified");
  else $("kout").innerHTML = '<span class="bad">' + esc(d.msg || "Invalid key") + "</span>";
}
async function saveAcct() {
  const body = {
    id: ($("kform").dataset.id || ""), label: ($("kalabel").value || "").trim(),
    apikey: ($("kak").value || "").trim(), zone_id: ($("kazone").value || "").trim(),
    subnet_id: ($("kasub").value || "").trim(), use: $("kause").checked,
  };
  if (!body.apikey) { toast("API key required"); return; }
  let d; try { d = await POST("/api/excloud/accounts/add", body); } catch (e) { toast("Failed"); return; }
  if (d.ok) { toast("Saved"); await loadAccounts(); keyModal(); loadMeta(); }
  else toast(d.msg || "Failed");
}

/* ---------------- meta / create form ---------------- */
function isWinImage(im) { return !!(im && im.windows); }
function typeLabel(t) {
  const rate = (t.rate != null) ? " - Rs." + Number(t.rate).toFixed(3) + "/hr" : "";
  return `${t.name} - ${t.cpu} vCPU/${t.ram}GB/${t.disk}GB${rate}`;
}
function fillTypes() {
  const types = META.types || [];
  $("c_type").innerHTML = types.map((t, i) => `<option value="${i}">${esc(typeLabel(t))}</option>`).join("");
}
function fillImages() {
  const imgs = META.images || [];
  $("c_image").innerHTML = imgs.map((im, i) => `<option value="${i}">${esc(im.name)}${isWinImage(im) ? " [Win]" : ""}</option>`).join("");
}
function onImageChange() {
  const im = (META.images || [])[parseInt($("c_image").value || "0", 10)] || {};
  const win = isWinImage(im);
  $("c_user").disabled = win; $("c_pass").disabled = win; $("c_testline").disabled = win;
  $("btn_test").disabled = win;
  $("btn_create").textContent = win ? "Create Server" : "Create Proxies";
  if (win && !$("c_name").value.startsWith("WIN")) $("c_name").value = "WIN01";
  else if (!win && $("c_name").value === "WIN01") $("c_name").value = "exproxy";
}
async function loadMeta() {
  let d; try { d = await J("/api/excloud/meta"); } catch (e) { return; }
  META = d || {}; META.images = d.images || []; META.types = d.types || []; META.subnets = d.subnets || [];
  fillImages();
  fillTypes();
  const subs = d.subnets || [];
  $("c_subnet").innerHTML = subs.length
    ? subs.map(s => `<option value="${esc(s.id)}"${String(s.id) === String(d.default_subnet_id) ? " selected" : ""}>${esc(s.name)} ${esc(s.cidr || "")}</option>`).join("")
    : '<option value="">(account default)</option>';
  onImageChange();
  loadTemplates();
  if (d.user) $("balance").textContent = "ExCloud | " + d.user;
}
async function loadTemplates() {
  let d; try { d = await J("/api/excloud/snaps"); } catch (e) { return; }
  SNAPS = d.snaps || [];
  const items = ['<option value="">None (fresh install)</option>'].concat(
    SNAPS.map((s, i) => `<option value="${i}">${esc(s.name)}  |  ${esc(s.vm_name || "")}  |  ${esc(s.date || "")}</option>`));
  $("c_tpl").innerHTML = items.join("");
}

async function createServers() {
  const ti = parseInt($("c_type").value || "0", 10);
  const t = (META.types || [])[ti] || {};
  const im = (META.images || [])[parseInt($("c_image").value || "0", 10)] || {};
  const tplIdx = $("c_tpl").value;
  let template = null;
  if (tplIdx !== "" && SNAPS[tplIdx]) template = { snapshot_id: SNAPS[tplIdx].snapshot_id };
  const body = {
    image: { id: im.id, windows: isWinImage(im) },
    instance_type: t.name,
    root_disk_gib: $("c_disk").value || 25,
    subnet_id: $("c_subnet").value || null,
    zone_id: 1,
    count: $("c_count").value, instance_name: ($("c_name").value || "").trim() || "exproxy",
    proxy_user: ($("c_user").value || "ajay").trim() || "ajay", proxy_pass: $("c_pass").value || "ajay",
    proxy_port: 3128, root_password: $("c_root").value,
    template: template,
  };
  if (template) body.template = template;
  body.hours = parseInt(($("c_hours") && $("c_hours").value) || "3", 10);
  let d; try { d = await POST("/api/excloud/create", body); } catch (e) { toast("Failed"); return; }
  if (d.ok) { toast("Creating... watch the log"); }
  else toast(d.msg || "Failed");
}

/* ---------------- servers ---------------- */
function proxyLine(r) {
  if (!r.ip) return "";
  return r.windows ? `${r.ip}:3389:Administrator:${$("c_root").value || ""}` : `${r.ip}:3128:${$("c_user").value || "ajay"}:${$("c_pass").value || "ajay"}`;
}
async function loadServers() {
  let d; try { d = await J("/api/excloud/state"); } catch (e) { return; }
  ROWS = d.rows || [];
  if (!ROWS.length) {
    $("srvBody").innerHTML = '<tr><td colspan="9" class="empty">No servers. Create some above.</td></tr>';
  } else {
    $("srvBody").innerHTML = ROWS.map((r, i) => `
      <tr data-i="${i}">
        <td class="chk"><input type="checkbox" class="rowchk" data-i="${i}"></td>
        <td>${esc(r.name)}</td>
        <td class="cd" data-dl="${r.deadline || 0}">-</td>
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
async function deleteSelected() {
  const sel = selectedRows();
  if (!sel.length) { toast("Select servers first"); return; }
  if (!confirm("Delete " + sel.length + " server(s)? Bill stops for them.")) return;
  const d = await POST("/api/excloud/delete", { ids: sel.map(r => r.vm_id) });
  toast(d.ok ? "Deleting... watch the log" : "Failed");
  setTimeout(loadServers, 2500);
}
async function deleteAll() {
  if (!confirm("Delete ALL excloud servers? Bill will become 0.")) return;
  const d = await POST("/api/excloud/delete", { all: true });
  toast(d.ok ? "Deleting all... watch the log" : "Failed");
  setTimeout(loadServers, 3000);
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
  const d = await POST("/api/excloud/snapshot", { vm_id: r.vm_id, vm_name: r.name, name });
  toast(d.ok ? "Snapshot started... watch the log" : (d.msg || "Failed"));
  setTimeout(loadTemplates, 4000);
}

/* ---------------- test proxy ---------------- */
async function testProxy() {
  const line = ($("c_testline").value || "").trim();
  if (!line) { toast("Enter ip:port:user:pass"); return; }
  toast("Testing " + line + " ...");
  let d; try { d = await POST("/api/excloud/testproxy", { line }); } catch (e) { toast("Failed"); return; }
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
  let d; try { d = await J("/api/excloud/output"); } catch (e) { return; }
  openModal("Output - " + d.file, d.text
    ? `<div class="mono">${esc(d.text)}</div><div style="margin-top:10px"><button class="btn sm" onclick="copyText(document.querySelector('.mono').textContent,'Copied')">Copy all</button></div>`
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
    const d = await J("/api/excloud/meta");
    if (d && d.user) $("balance").textContent = "ExCloud | " + d.user;
  } catch (e) { /* ignore */ }
  setTimeout(pollInfo, 30000);
}

/* ---------------- init ---------------- */
function wire() {
  $("c_image").addEventListener("change", onImageChange);
  $("c_tpl").addEventListener("change", () => { /* template selected */ });
  $("ex_acct").addEventListener("change", acctChanged);
  $("btn_keys").addEventListener("click", keyModal);
  $("modal-x").addEventListener("click", closeModal);
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  $("btn_create").addEventListener("click", createServers);
  $("btn_delall").addEventListener("click", deleteAll);
  $("btn_snap").addEventListener("click", snapModal);
  $("btn_test").addEventListener("click", testProxy);
  $("btn_open").addEventListener("click", openOutput);
  $("btn_refresh").addEventListener("click", loadServers);
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
