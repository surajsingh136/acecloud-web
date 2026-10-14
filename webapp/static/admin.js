/* CloudWeb - admin panel frontend */
"use strict";

var PASS = sessionStorage.getItem("adm_pass") || "";
var USERS = {};

function $(id) { return document.getElementById(id); }
function toast(m) {
  var t = $("toast"); t.textContent = m; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(function () { t.style.display = "none"; }, 2000);
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}
function btn(label, cls, js) {
  return '<button class="mini ' + (cls || "") + '" onclick="' + js + '">' + esc(label) + "</button>";
}

async function api(action, body) {
  body = body || {};
  body.pass = PASS;
  var r = await fetch("/api/admin/" + action, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  var d = await r.json().catch(function () { return {}; });
  if (r.status === 401) { lock("Wrong admin password"); throw new Error("auth"); }
  return d;
}

function unlock() {
  PASS = $("gatePass").value;
  sessionStorage.setItem("adm_pass", PASS);
  load();
}

function lock(msg) {
  $("dash").style.display = "none";
  $("gate").style.display = "block";
  $("gateMsg").textContent = msg || "";
}

async function load() {
  var d;
  try { d = await api("list"); }
  catch (e) { return; }
  if (!d || !d.ok) { lock(d && d.msg ? d.msg : "Not authorised"); return; }
  $("gate").style.display = "none";
  $("gateMsg").textContent = "";
  $("dash").style.display = "block";
  $("sPersist").textContent = d.persist ? "GitHub (persistent)" : "local (resets on restart)";
  var users = d.users || [];
  U = {};
  users.forEach(function (u) { U[u.id] = u; });
  $("sTotal").textContent = users.length;
  $("sPending").textContent = users.filter(function (u) { return u.status === "pending"; }).length;
  $("sApproved").textContent = users.filter(function (u) { return u.status === "approved"; }).length;
  render(users);
}

var U = {};

function render(users) {
  if (!users.length) { $("list").innerHTML = '<div class="empty">No users yet.</div>'; return; }
  var rows = users.map(function (u) {
    var st = u.status || "pending";
    var id = u.id;
    var a = "";
    if (st === "pending") {
      a += btn("Approve", "ok", "act('approve','" + id + "')");
      a += btn("Reject", "no", "act('reject','" + id + "')");
    } else if (st === "approved") {
      a += btn("Reject", "no", "act('reject','" + id + "')");
    } else if (st === "rejected") {
      a += btn("Approve", "ok", "act('approve','" + id + "')");
    }
    a += btn("Reset password", "cy", "resetPass('" + id + "')");
    if (u.role !== "admin") { a += btn("Delete", "no", "del('" + id + "')"); }
    return "<tr>" +
      "<td>" + esc(u.email) + "</td>" +
      "<td>" + esc(u.name) + "</td>" +
      "<td>" + (u.role === "admin" ? '<span class="badge b-admin">admin</span> ' : "") +
        '<span class="badge b-' + esc(st) + '">' + esc(st) + "</span></td>" +
      "<td>" + esc(u.created) + "</td>" +
      "<td>" + esc(u.last_login || "-") + "</td>" +
      "<td>" + a + "</td></tr>";
  }).join("");
  $("list").innerHTML =
    "<table><thead><tr><th>Email</th><th>Name</th><th>Status</th><th>Created</th>" +
    "<th>Last login</th><th>Actions</th></tr></thead><tbody>" + rows + "</tbody></table>";
}

async function act(action, id) {
  try { await api(action, { id: id }); toast(action === "approve" ? "Approved" : "Rejected"); load(); }
  catch (e) {}
}

async function del(id) {
  var email = (U[id] && U[id].email) || id;
  if (!confirm("Delete user " + email + " ? This removes their accounts too.")) return;
  try { await api("delete", { id: id }); toast("Deleted"); load(); }
  catch (e) {}
}

function resetPass(id) {
  var pw = prompt("New password (min 6 characters):");
  if (pw === null) return;
  if (pw.length < 6) { toast("Too short"); return; }
  api("pass", { id: id, password: pw }).then(function () { toast("Password updated"); }).catch(function () {});
}

if (PASS) load();
