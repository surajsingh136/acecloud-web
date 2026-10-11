/* ACE Hub - /all frontend */
"use strict";

const $ = (id) => document.getElementById(id);

/* ---- Sites shown on the hub. Add more websites here later. ---- */
const SITES = [
  { id: "acecloud", name: "AceCloud", logo: "/logo.svg",
    desc: "Windows Spot & Linux proxy VMs", config: true, url: "/" },
  // Example for future websites:
  // { id: "other", name: "Other Tool", logo: "/logo.svg",
  //   desc: "Short description", config: false, url: "https://example.com" },
];

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

/* ---------------- ui helpers ---------------- */
function toast(m) { const t = $("toast"); t.textContent = m; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 1900); }
function hide(id) { $(id).style.display = "none"; }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ---------------- hub ---------------- */
function renderSites() {
  $("sites").innerHTML = SITES.map(s => `
    <div class="card" onclick="openSite('${s.id}')">
      <img class="logo" src="${s.logo}" alt="${esc(s.name)}">
      <div class="cname">${esc(s.name)}</div>
      <div class="cdesc">${esc(s.desc)}</div>
      <span class="chip ${s.config ? "" : "go"}">${s.config ? "Configure &amp; run" : "Open"}</span>
    </div>`).join("");
}

function openSite(id) {
  const s = SITES.find(x => x.id === id);
  if (!s) return;
  if (s.config) openConfig(s);
  else window.location.href = s.url;
}

function addPlaceholder() { toast("Coming soon - more websites will be added here."); }

/* ---------------- account config modal ---------------- */
let EDIT_ID = "";
let ACC = {};

async function openConfig(site) {
  $("cfgTitle").textContent = site.name + " - Configure";
  resetForm();
  $("cfg").style.display = "flex";
  await loadAccounts();
}

function resetForm() {
  EDIT_ID = "";
  $("formTitle").textContent = "Add account";
  ["alabel", "aemail", "apass", "akey", "anote"].forEach(i => { $(i).value = ""; });
  $("ause").checked = true;
}

async function loadAccounts() {
  let d;
  try { d = await J("/api/accounts"); }
  catch (e) { $("acclist").innerHTML = '<div class="err">PIN required.</div>'; return; }
  ACC = {};
  const list = d.accounts || [];
  list.forEach(a => { ACC[a.id] = a; });
  $("acclist").innerHTML =
    '<table class="acct"><thead><tr><th>Use</th><th>Label</th><th>Email</th><th>API</th><th></th></tr></thead><tbody>' +
    list.map(a => `
      <tr class="${a.id === d.active ? "act" : ""}">
        <td>${a.id === d.active ? '<span class="dot" title="active"></span>'
             : `<button class="mini" onclick="activate('${a.id}')">Use</button>`}</td>
        <td>${esc(a.label)}${a.editable ? "" : ' <span class="ro">(read-only)</span>'}</td>
        <td>${esc(a.email)}</td>
        <td>${a.api_key ? "set" : "-"}</td>
        <td class="rowbtns">
          ${a.editable ? `<button class="mini" onclick="editAccount('${a.id}')">Edit</button>
          <button class="mini del" onclick="delAccount('${a.id}')">Del</button>` : ""}
          <button class="mini go" onclick="window.location.href='/'">Open</button>
        </td>
      </tr>`).join("") +
    "</tbody></table>";
  const act = list.find(a => a.id === d.active) || {};
  $("activeInfo").textContent = "Active account: " + (act.email || "-") + (act.label ? " (" + act.label + ")" : "");
}

async function saveAccount() {
  const body = {
    id: EDIT_ID, label: $("alabel").value.trim(), email: $("aemail").value.trim(),
    password: $("apass").value, api_key: $("akey").value.trim(),
    note: $("anote").value.trim(), use: $("ause").checked,
  };
  if (!EDIT_ID && (!body.email || !body.password)) { toast("Email and password required"); return; }
  let d;
  try { d = await POST("/api/accounts/add", body); }
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
  $("aemail").value = a.email || "";
  $("apass").value = "";
  $("akey").value = a.api_key || "";
  $("anote").value = a.note || "";
  $("cfg").scrollTop = 0;
}

async function delAccount(id) {
  if (!confirm("Delete this account?")) return;
  const d = await POST("/api/accounts/delete", { id });
  toast(d.ok ? "Deleted" : (d.msg || "Failed"));
  loadAccounts();
}

async function activate(id) {
  const d = await POST("/api/accounts/activate", { id });
  toast(d.ok ? "Active account switched" : (d.msg || "Failed"));
  loadAccounts();
}

renderSites();
