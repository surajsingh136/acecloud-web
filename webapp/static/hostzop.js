/* HOSTZOP Manager - frontend */
"use strict";
const $ = (id) => document.getElementById(id);
let EDIT_ID = "", ACC = {}, META = { sizes: [] }, LASTN = 0;

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

function toast(m) { const t = $("toast"); t.textContent = m; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 2200); }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function showTab(t) {
  document.querySelectorAll(".tab").forEach(el => el.classList.toggle("on", el.dataset.t === t));
  document.querySelectorAll(".panel").forEach(el => el.classList.toggle("on", el.id === "p-" + t));
  if (t === "servers") loadServers();
  if (t === "create") loadMeta();
}

/* ---------------- accounts ---------------- */
async function loadAccounts() {
  let d;
  try { d = await J("/api/hostzop/accounts"); }
  catch (e) { $("acclist").innerHTML = '<div class="bad">PIN required.</div>'; return; }
  ACC = {};
  const list = d.accounts || [];
  list.forEach(a => { ACC[a.id] = a; });
  $("acclist").innerHTML =
    '<table class="acct"><thead><tr><th>Use</th><th>Label</th><th>API key</th><th>Billing</th><th>Location</th><th></th></tr></thead><tbody>' +
    list.map(a => `
      <tr class="${a.id === d.active ? "act" : ""}">
        <td>${a.id === d.active ? '<span class="dot" title="active"></span>'
             : `<button class="mini" onclick="activate('${a.id}')">Use</button>`}</td>
        <td>${esc(a.label)}${a.editable ? "" : ' <span class="ro">(read-only)</span>'}</td>
        <td>${a.has_key ? "set" : "-"}</td>
        <td>${esc(a.billing_account_id || "-")}</td>
        <td>${esc(a.location || "-")}</td>
        <td class="rowbtns">
          ${a.editable ? `<button class="mini" onclick="editAccount('${a.id}')">Edit</button>
          <button class="mini del" onclick="delAccount('${a.id}')">Del</button>` : ""}
        </td>
      </tr>`).join("") + "</tbody></table>";
  const act = list.find(a => a.id === d.active) || {};
  $("activeInfo").textContent = "Active account: " + (act.label || "-") +
    (act.location ? " @ " + act.location : "");
  if (act && act.billing_account_id) $("c_bill").value = act.billing_account_id;
}

function resetForm() {
  EDIT_ID = "";
  $("formTitle").textContent = "Add account";
  ["alabel", "akey", "abill", "anote"].forEach(i => { $(i).value = ""; });
  $("aloc").value = "";
  $("ause").checked = true;
  $("verifyOut").textContent = "";
}

function fillLocations(locs, selected) {
  const sel = $("aloc");
  sel.innerHTML = '<option value="">(default)</option>' +
    (locs || []).map(l => `<option value="${esc(l.slug)}">${esc(l.name)}${l.is_default ? " (default)" : ""}</option>`).join("");
  if (selected) sel.value = selected;
}

async function verifyKey() {
  const key = $("akey").value.trim();
  if (!key) { toast("Enter API key first"); return; }
  $("verifyOut").textContent = "Verifying...";
  let d;
  try { d = await POST("/api/hostzop/verify", { apikey: key }); }
  catch (e) { $("verifyOut").textContent = "Failed"; return; }
  if (d.ok) {
    $("verifyOut").innerHTML = '<span class="ok">OK</span> - ' + esc(d.user || d.email || "verified") +
      " | locations: " + (d.locations || []).length;
    fillLocations(d.locations, $("aloc").value);
  } else {
    $("verifyOut").innerHTML = '<span class="bad">' + esc(d.msg || "Invalid key") + "</span>";
  }
}

async function saveAccount() {
  const body = {
    id: EDIT_ID, label: $("alabel").value.trim(), apikey: $("akey").value.trim(),
    billing_account_id: $("abill").value.trim(), location: $("aloc").value,
    note: $("anote").value.trim(), use: $("ause").checked,
  };
  if (!body.apikey) { toast("API key required"); return; }
  let d;
  try { d = await POST("/api/hostzop/accounts/add", body); }
  catch (e) { toast("Failed"); return; }
  if (d.ok) { toast("Saved"); resetForm(); loadAccounts(); }
  else toast(d.msg || "Failed");
}

function editAccount(id) {
  const a = ACC[id];
  if (!a) return;
  EDIT_ID = id;
  $("formTitle").textContent = "Edit account";
  $("alabel").value = a.label || "";
  $("akey").value = a.apikey || "";
  $("abill").value = a.billing_account_id || "";
  $("anote").value = a.note || "";
  $("ause").checked = false;
  if (a.locations) fillLocations(a.locations, a.location);
  else fillLocations([], a.location);
  window.scrollTo(0, 0);
}

async function delAccount(id) {
  if (!confirm("Delete this account?")) return;
  const d = await POST("/api/hostzop/accounts/delete", { id });
  toast(d.ok ? "Deleted" : (d.msg || "Failed"));
  loadAccounts();
}

async function activate(id) {
  const d = await POST("/api/hostzop/accounts/activate", { id });
  toast(d.ok ? "Active account switched" : (d.msg || "Failed"));
  loadAccounts(); loadMeta();
}

/* ---------------- create / meta ---------------- */
function osVersions(os) {
  const imgs = (META.images || []);
  const find = (n) => (imgs.find(x => (x.name || "").toLowerCase() === n) || {}).versions || [];
  if (os === "windows") return ["2019", "2022", "2025"];
  if (os === "ubuntu") {
    const vs = find("ubuntu");
    const majors = ["20", "22", "24"].filter(m => vs.some(v => String(v).startsWith(m + ".")));
    return majors.length ? majors : ["20", "22", "24"];
  }
  return find(os).length ? find(os) : ["8"];
}
function onOsChange() {
  const os = $("c_os").value;
  const vers = osVersions(os);
  $("c_ver").innerHTML = vers.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  if (os === "ubuntu" && vers.includes("22")) $("c_ver").value = "22";
  if (os === "windows") $("c_ver").value = vers.includes("2022") ? "2022" : vers[0];
}

async function loadMeta() {
  let d;
  try { d = await J("/api/hostzop/meta"); } catch (e) { return; }
  META = d || {};
  if (d.locations) fillLocations(d.locations, $("aloc").value);
  const pools = d.pools || [];
  $("c_pool").innerHTML = pools.length
    ? pools.map(p => `<option value="${esc(p.uuid)}"${p.default ? " selected" : ""}>${esc(p.name)}${p.default ? " (default)" : ""}</option>`).join("")
    : '<option value="">(account default)</option>';
  const sizes = d.sizes || [];
  META.sizes = sizes;
  $("c_size").innerHTML = sizes.map((s, i) =>
    `<option value="${i}">${esc(s.name)} - ${s.cpu} vCPU/${s.ram / 1024}GB/${s.disk}GB${s.rate != null ? " - Rs." + s.rate.toFixed(2) + "/hr" : ""}</option>`).join("");
  onOsChange();
  $("metaOut").innerHTML = d.user ? ("Connected as <b>" + esc(d.user) + "</b>") : "";
  if (d.billing_account_id) $("c_bill").value = d.billing_account_id;
}

async function createServers() {
  const si = parseInt($("c_size").value || "0", 10);
  const sz = (META.sizes || [])[si] || {};
  const body = {
    os_name: $("c_os").value, os_version: $("c_ver").value,
    vcpu: sz.cpu, ram: sz.ram, disks: sz.disk,
    count: $("c_count").value, instance_name: $("c_name").value.trim() || "hzproxy",
    network_name: $("c_net").value.trim() || "My Network",
    proxy_user: $("c_user").value.trim() || "ajay", proxy_pass: $("c_pass").value || "ajay",
    proxy_port: parseInt($("c_port").value || "3128", 10),
    root_password: $("c_root").value, billing_account_id: $("c_bill").value.trim(),
    designated_pool_uuid: $("c_pool").value, reserve_public_ip: $("c_pubip").checked,
  };
  let d;
  try { d = await POST("/api/hostzop/create", body); }
  catch (e) { toast("Failed"); return; }
  if (d.ok) { toast("Creating... see Logs"); showTab("logs"); }
  else toast(d.msg || "Failed");
}

/* ---------------- servers ---------------- */
let ROWS = [];
async function loadServers() {
  let d;
  try { d = await J("/api/hostzop/state"); } catch (e) { return; }
  ROWS = d.rows || [];
  $("srvBody").innerHTML = ROWS.map((r, i) => {
    const proxy = r.ip ? (r.windows ? `${r.ip}:3389:Administrator:****` : `${r.ip}:3128:ajay:ajay`) : "";
    return `<tr>
      <td><input type="checkbox" class="rowchk" data-i="${i}"></td>
      <td>${esc(r.name)}</td><td>${esc(r.ip || "-")}</td>
      <td>${esc(r.status)}</td><td>${esc(r.os)}</td><td>${esc(r.size)}</td>
      <td>${esc(proxy || "-")}</td><td>${esc(r.created)}</td>
    </tr>`;
  }).join("");
  const running = ROWS.filter(r => ["running", "active", "ready"].includes(r.status.toLowerCase())).length;
  const withip = ROWS.filter(r => r.ip).length;
  $("serverStats").textContent = `Servers: ${ROWS.length} | Running: ${running} | With IP: ${withip}`;
}

function toggleAll(cb) { document.querySelectorAll(".rowchk").forEach(c => { c.checked = cb.checked; }); }
function selectedRows() {
  const out = [];
  document.querySelectorAll(".rowchk").forEach(c => { if (c.checked) out.push(ROWS[parseInt(c.dataset.i, 10)]); });
  return out;
}
function copySelected(kind) {
  const sel = selectedRows();
  const r = sel[0] || ROWS[0];
  if (!r) { toast("No server"); return; }
  let txt = kind === "ip" ? r.ip : (r.ip ? (r.windows ? `${r.ip}:3389:Administrator:${PROXYROOT || ""}` : `${r.ip}:3128:ajay:ajay`) : "");
  if (!txt) { toast("No value"); return; }
  navigator.clipboard.writeText(txt).then(() => toast("Copied: " + txt), () => toast("Copy failed"));
}
let PROXYROOT = "";
async function deleteSelected() {
  const sel = selectedRows();
  if (!sel.length) { toast("Select servers first"); return; }
  if (!confirm("Delete " + sel.length + " server(s)? Bill stops for them.")) return;
  const d = await POST("/api/hostzop/delete", { uuids: sel.map(r => r.uuid) });
  toast(d.ok ? "Deleting... see Logs" : "Failed");
  setTimeout(loadServers, 1500);
}
async function deleteAll() {
  if (!confirm("Delete ALL hostzop servers? Bill will become 0.")) return;
  const d = await POST("/api/hostzop/delete", { all: true });
  toast(d.ok ? "Deleting all... see Logs" : "Failed");
  setTimeout(loadServers, 2000);
}

/* ---------------- logs ---------------- */
async function pollLogs() {
  try {
    const d = await J("/api/logs?since=" + LASTN);
    if (d.logs && d.logs.length) {
      const box = $("logbox");
      d.logs.forEach(l => {
        const cls = l.k === "error" ? "e" : l.k === "ok" ? "o" : l.k === "warn" ? "w" : l.k === "sys" ? "s" : "";
        box.innerHTML += `<div class="${cls}">[${esc(l.t)}] ${esc(l.m)}</div>`;
      });
      box.scrollTop = box.scrollHeight;
      LASTN = d.n;
    }
  } catch (e) { /* ignore */ }
  setTimeout(pollLogs, 1500);
}

/* ---------------- init ---------------- */
(async function () {
  await loadAccounts();
  await loadMeta();
  fillLocations([], "");
  pollLogs();
})();
