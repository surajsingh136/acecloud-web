/* AceCloud Web Manager v2 - frontend */
"use strict";

const $ = (id) => document.getElementById(id);
let ROWS = [], SEL = new Set(), logN = 0;

/* ---------------- transport ---------------- */
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
const POST = (url, obj) => J(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) });

/* ---------------- ui helpers ---------------- */
function toast(m) { const t = $("toast"); t.textContent = m; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 1800); }
function copyTxt(t, msg) { navigator.clipboard.writeText(t).then(() => toast(msg || "Copied!")); }
function hide(id) { $(id).style.display = "none"; }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function addLog(m, k) {
  const box = $("logbox");
  const d = document.createElement("div");
  d.className = "l-" + (k || "info");
  d.textContent = "[" + new Date().toLocaleTimeString("en-GB") + "] " + m;
  box.appendChild(d);
  while (box.children.length > 400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

/* ---------------- data ---------------- */
async function loadSizes() {
  const q = new URLSearchParams({ type: $("type").value, region: $("region").value, billing: $("billing").value });
  try {
    const d = await J("/api/sizes?" + q);
    if (d.error) { addLog("sizes fail: " + d.error, "error"); return; }
    $("size").innerHTML = d.sizes.map(s => `<option>${esc(s)}</option>`).join("");
    $("base").innerHTML = d.bases.map(s => `<option>${esc(s)}</option>`).join("");
  } catch (e) { addLog("sizes fail: " + e.message, "error"); }
}

async function pollState() {
  let d;
  try { d = await J("/api/state"); } catch (e) { return; }
  const c = $("conn");
  if (d.logged) { c.textContent = "CONNECTED"; c.classList.add("on"); }
  else { c.textContent = "DISCONNECTED - Refresh"; c.classList.remove("on"); }
  document.body.classList.toggle("busy", d.busy);
  ["btnCreate", "btnDelAll", "btnDelSel", "btnSnap", "btnTest"].forEach(id => { const b = $(id); if (b) b.disabled = d.busy; });

  ROWS = d.instances;
  SEL = new Set([...SEL].filter(i => i < ROWS.length));
  $("tb").innerHTML = ROWS.map((r, i) => `
    <tr class="${SEL.has(i) ? "sel" : ""}" onclick="tog(${i})" ondblclick="copyTxt('${esc(r.ip)}','IP copied')">
      <td onclick="event.stopPropagation()"><input type="checkbox" ${SEL.has(i) ? "checked" : ""} onchange="tog(${i})"></td>
      <td class="st ${esc(r.status)}">&#9679; ${esc(r.status)}</td>
      <td>${esc(r.name)}</td><td>${esc(r.ip)}</td><td>${esc(r.size)}</td>
      <td>${esc(r.region_label)}</td><td class="${esc(r.type)}">${esc(r.type)}</td>
      <td>${esc(r.pricing)}</td><td>${esc(r.plan)}</td>
    </tr>`).join("") || `<tr><td colspan="9" style="text-align:center;color:#8a97a3;padding:56px;font-size:26px">No instances (or not logged in)</td></tr>`;

  $("stats").textContent = `Servers: ${d.counts.total}   |   SPOT: ${d.counts.spot}   |   ON-DEMAND: ${d.counts.ondemand}   |   PROXY: ${d.counts.proxy}`;
  const bal = d.balance == null ? "Balance: --" : `Balance: Rs.${(+d.balance).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;
  $("bal").textContent = `${bal} | Total: ${d.counts.total} | Active: ${d.counts.active}`;
}

async function pollLogs() {
  try {
    const d = await J("/api/logs?since=" + logN);
    if (d.n > logN) { d.logs.forEach(l => addLog(l.m, l.k)); logN = d.n; }
  } catch (e) { /* ignore */ }
}

/* ---------------- actions ---------------- */
function tog(i) { SEL.has(i) ? SEL.delete(i) : SEL.add(i); pollState(); }
function ckAll() { const on = $("ckall").checked; SEL = on ? new Set(ROWS.map((_, i) => i)) : new Set(); pollState(); }

async function doCreate() {
  const d = await POST("/api/create", {
    type: $("type").value, region: $("region").value, billing: $("billing").value,
    count: +$("count").value, hours: +$("hours").value, port: +$("port").value,
    size_idx: $("size").selectedIndex, base: $("base").value,
    user: $("puser").value, pass: $("ppass").value,
  });
  toast(d.ok ? "Create started... (see activity log)" : (d.msg || "Fail"));
}

async function delAll() {
  if (!ROWS.length) { toast("No instances."); return; }
  if (!confirm(`Delete ALL ${ROWS.length} instance(s)?\nBilling will stop for them.`)) return;
  const d = await POST("/api/delete", { all: true });
  toast(d.ok ? "Delete started..." : (d.msg || "Busy..."));
}

async function delSel() {
  if (!SEL.size) { toast("Select row(s) first (Ctrl+click for multi)"); return; }
  const items = [...SEL].map(i => ({ region: ROWS[i].region, id: ROWS[i].id, name: ROWS[i].name }));
  if (!confirm(`Delete ${items.length} instance(s)?`)) return;
  const d = await POST("/api/delete", { items });
  toast(d.ok ? "Delete started..." : (d.msg || "Busy..."));
}

function openSnap() {
  const eligible = ROWS.filter(r => ["ACTIVE", "SHUTOFF", "STOPPED"].includes(r.status));
  if (!eligible.length) { toast("No snapshot-eligible instance"); return; }
  $("snapinst").innerHTML = eligible.map((r, i) => `<option value="${i}">[${esc(r.region_label)}] ${esc(r.name)}</option>`).join("");
  $("snapinst")._rows = eligible;
  $("snapdlg").style.display = "flex";
}

async function doSnap() {
  const r = $("snapinst")._rows[+$("snapinst").value];
  hide("snapdlg");
  const d = await POST("/api/snapshot", { region: r.region, name: r.name, snap_name: $("snapname").value });
  toast(d.ok ? "Snapshot started..." : (d.msg || "Busy..."));
}

function copyAllIps() {
  const ips = ROWS.filter(r => r.status === "ACTIVE" && r.ip).map(r => r.ip);
  if (!ips.length) { toast("No ACTIVE instance."); return; }
  copyTxt(ips.join("\n"), ips.length + " running IPs copied");
}

function sel1() {
  if (!SEL.size) { toast("Select a row first"); return null; }
  return ROWS[[...SEL][0]];
}

async function copyIP() { const r = sel1(); if (r && r.ip) copyTxt(r.ip, "IP copied"); }

async function copyPass() {
  const r = sel1(); if (!r) return;
  const d = await J("/api/creds?ip=" + encodeURIComponent(r.ip));
  if (d.pass) copyTxt(d.pass, "Password copied");
  else toast("No password saved for this row (Linux proxy?).");
}

async function copyProxy() {
  const r = sel1(); if (!r) return;
  const d = await J("/api/proxyline?ip=" + encodeURIComponent(r.ip));
  if (d.line) copyTxt(d.line, "Proxy copied");
  else toast("No proxy string found for this IP in the proxy list.");
}

async function openConsole() {
  const r = sel1(); if (!r) return;
  const d = await J("/api/creds?ip=" + encodeURIComponent(r.ip));
  if (d.console) window.open(d.console, "_blank");
  else toast("Console URL not found.");
}

async function doTest() {
  const line = $("testline").value.trim();
  if (!line) { toast("Enter ip:port:user:pass"); return; }
  const d = await POST("/api/test", { line });
  toast(d.ok ? "Testing... (log me result)" : (d.msg || "Busy..."));
}

async function refreshAll() { await POST("/api/refresh", {}); toast("Refreshing..."); pollState(); }

/* ---------------- boot ---------------- */
setInterval(pollLogs, 1200);
setInterval(() => {
  pollState();
  $("clock").textContent = new Date().toLocaleString("en-GB");
}, 3000);
pollState();
pollLogs();
loadSizes();
