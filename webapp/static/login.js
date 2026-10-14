/* CloudWeb - login & signup frontend */
"use strict";

function setMsg(id, text, kind) {
  var e = document.getElementById(id);
  if (!e) return;
  e.className = "msg" + (kind ? " " + kind : "");
  e.textContent = text || "";
}

function tab(which) {
  var isUp = which === "up";
  document.getElementById("fIn").style.display = isUp ? "none" : "";
  document.getElementById("fUp").style.display = isUp ? "" : "none";
  document.getElementById("tabIn").className = isUp ? "" : "on";
  document.getElementById("tabUp").className = isUp ? "on" : "";
}

async function doLogin(ev) {
  ev.preventDefault();
  var email = document.getElementById("inEmail").value.trim();
  var password = document.getElementById("inPass").value;
  setMsg("inMsg", "", "");
  if (!password) { setMsg("inMsg", "Enter your password", "err"); return false; }
  var btn = document.getElementById("inBtn");
  btn.disabled = true; btn.textContent = "Signing in...";
  try {
    var r = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email, password: password })
    });
    var d = await r.json().catch(function () { return {}; });
    if (r.ok && d.ok) { location.href = "/"; return false; }
    setMsg("inMsg", d.msg || "Sign in failed", d.status ? "warn" : "err");
  } catch (e) {
    setMsg("inMsg", "Network error, try again", "err");
  }
  btn.disabled = false; btn.textContent = "Sign in";
  return false;
}

async function doSignup(ev) {
  ev.preventDefault();
  var name = document.getElementById("upName").value.trim();
  var email = document.getElementById("upEmail").value.trim();
  var password = document.getElementById("upPass").value;
  setMsg("upMsg", "", "");
  var btn = document.getElementById("upBtn");
  btn.disabled = true; btn.textContent = "Creating...";
  try {
    var r = await fetch("/api/auth/signup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, email: email, password: password })
    });
    var d = await r.json().catch(function () { return {}; });
    if (r.ok && d.ok) {
      setMsg("upMsg", "Account created. Please wait for the admin to approve it.", "ok");
      document.getElementById("fUp").reset();
    } else {
      setMsg("upMsg", d.msg || "Signup failed", "err");
    }
  } catch (e) {
    setMsg("upMsg", "Network error, try again", "err");
  }
  btn.disabled = false; btn.textContent = "Create account";
  return false;
}
