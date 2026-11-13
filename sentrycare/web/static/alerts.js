"use strict";
// Live fall alerts on every dashboard: full-screen alert, siren, system notification, vibration.
// Any acknowledgement (on any screen) is broadcast by the server and clears the alert everywhere.
const alerting = {source:null, active:new Map(), audio:null, sirenTimer:null, wantSound:false,
  history:[], settings:null, flashTimer:null, baseTitle:document.title, swReady:null};

try{ alerting.wantSound = localStorage.getItem("sentrycare-alerts") === "on"; }catch{}

// ---- sound -------------------------------------------------------------------------------
function unlockAudio(){
  try{
    alerting.audio = alerting.audio || new (window.AudioContext || window.webkitAudioContext)();
    if(alerting.audio.state === "suspended") alerting.audio.resume();
  }catch{}
}
function tone(freq, start, length, gain=.22){
  const ctx = alerting.audio, osc = ctx.createOscillator(), amp = ctx.createGain();
  osc.type = "square"; osc.frequency.setValueAtTime(freq, start);
  amp.gain.setValueAtTime(0, start); amp.gain.linearRampToValueAtTime(gain, start + .02);
  amp.gain.setValueAtTime(gain, start + length - .03); amp.gain.linearRampToValueAtTime(0, start + length);
  osc.connect(amp).connect(ctx.destination); osc.start(start); osc.stop(start + length);
}
function sirenBurst(){
  if(!alerting.audio || alerting.audio.state !== "running") return;
  const t = alerting.audio.currentTime;
  for(let i = 0; i < 4; i++){ tone(880, t + i * .5, .25); tone(660, t + i * .5 + .25, .25); }
}
function updateSiren(){
  const ringing = [...alerting.active.values()].some(a => !a.acknowledged);
  if(ringing && !alerting.sirenTimer){ sirenBurst(); alerting.sirenTimer = setInterval(sirenBurst, 2000); }
  if(!ringing && alerting.sirenTimer){ clearInterval(alerting.sirenTimer); alerting.sirenTimer = null; }
  if(ringing && !alerting.flashTimer){
    let on = false;
    alerting.flashTimer = setInterval(() => { on = !on; document.title = on ? "⚠ FALL DETECTED" : alerting.baseTitle; }, 800);
  }
  if(!ringing && alerting.flashTimer){ clearInterval(alerting.flashTimer); alerting.flashTimer = null; document.title = alerting.baseTitle; }
}

// ---- notifications -------------------------------------------------------------------------
function registerWorker(){
  if(!alerting.swReady && "serviceWorker" in navigator && window.isSecureContext)
    alerting.swReady = navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready).catch(() => null);
  return alerting.swReady || Promise.resolve(null);
}
async function systemNotify(alert){
  if(!("Notification" in window) || Notification.permission !== "granted") return;
  const options = {body:`${alert.person} · ${alert.camera} · ${new Date(alert.time).toLocaleTimeString()}`,
    tag:alert.id, requireInteraction:true, renotify:true, vibrate:[400,200,400,200,800]};
  try{
    const reg = await registerWorker();
    if(reg) await reg.showNotification("⚠ " + alert.title, options);
    else new Notification("⚠ " + alert.title, options);
  }catch{}
}
async function enableAlerts(){
  unlockAudio(); alerting.wantSound = true;
  try{ localStorage.setItem("sentrycare-alerts", "on"); }catch{}
  if("Notification" in window && Notification.permission === "default"){
    try{ await Notification.requestPermission(); }catch{}
  }
  registerWorker();
  if(alerting.audio){ const t = alerting.audio.currentTime; tone(880, t, .12, .12); tone(1175, t + .14, .16, .12); }
  renderAlertToggle();
  toast(("Notification" in window && Notification.permission === "granted") ? "Alerts on: sound and notifications" :
        window.isSecureContext ? "Alerts on: sound only (notifications blocked in browser settings)" : "Alerts on: sound only");
}
function renderAlertToggle(){
  const button = $("alerts-toggle"); if(!button) return;
  const sound = alerting.audio && alerting.audio.state === "running";
  const notes = "Notification" in window && Notification.permission === "granted";
  button.className = "pill " + (sound ? "" : "amber");
  button.innerHTML = `<i></i>${sound ? (notes ? "Alerts on" : "Alerts on · sound") : "Enable alerts"}`;
}

// ---- overlay ---------------------------------------------------------------------------------
function renderOverlay(){
  const pending = [...alerting.active.values()].filter(a => !a.acknowledged).sort((a,b) => b.epoch - a.epoch);
  const box = $("alert-overlay");
  if(!pending.length){ box.hidden = true; updateSiren(); return; }
  const a = pending[0];
  box.hidden = false;
  box.innerHTML = `<div class="alert-card" role="alertdialog" aria-labelledby="alert-title" aria-describedby="alert-body">
    <div class="alert-head"><span class="alert-icon">⚠</span><div><h2 id="alert-title">${esc(a.title)}</h2>
    <p id="alert-body"><b>${esc(a.person)}</b> · ${esc(a.camera)} · ${esc(new Date(a.time).toLocaleTimeString())}</p></div></div>
    ${a.snapshot ? `<img class="alert-snap" src="/api/alerts/${encodeURIComponent(a.id)}/snapshot.jpg" alt="Camera image when the fall was detected">` : ""}
    ${alerting.audio && alerting.audio.state === "running" ? "" : '<p class="alert-hint">Tap anywhere to turn on the alarm sound on this device.</p>'}
    <div class="alert-actions"><button class="button danger-button" data-ack="${esc(a.id)}">Acknowledge: I'm checking on them</button></div>
    ${pending.length > 1 ? `<p class="alert-more">+${pending.length - 1} more unacknowledged alert(s)</p>` : ""}
    <p class="alert-note">Research prototype: confirm in person. Acknowledging clears this alert on every screen.</p></div>`;
  updateSiren();
}

function receive(alert, fresh){
  alerting.active.set(alert.id, alert);
  upsertHistory(alert);
  renderOverlay();
  if(fresh && !alert.acknowledged){
    systemNotify(alert);
    try{ navigator.vibrate?.([400,200,400,200,800]); }catch{}
  }
}
function upsertHistory(alert){
  const i = alerting.history.findIndex(a => a.id === alert.id);
  if(i >= 0) alerting.history[i] = alert; else alerting.history.unshift(alert);
  renderAlertHistory();
}

// ---- event history tab: recent alerts + settings ----------------------------------------------
function renderAlertHistory(){
  const box = $("alerts-list"); if(!box) return;
  box.innerHTML = alerting.history.length ? alerting.history.slice(0, 20).map(a => `<div class="alert-row${a.acknowledged ? "" : " open"}">
      ${a.snapshot ? `<img src="/api/alerts/${encodeURIComponent(a.id)}/snapshot.jpg" alt="">` : '<span class="alert-noimg">⚠</span>'}
      <div><strong>${esc(a.title)}</strong><small>${esc(a.person)} · ${esc(a.camera)} · ${esc(new Date(a.time).toLocaleString())}</small>
      <small>${a.acknowledged ? `Acknowledged by ${esc(a.acknowledged.by)} at ${esc(new Date(a.acknowledged.time).toLocaleTimeString())}` : "<b>Waiting for acknowledgement</b>"}</small></div>
      ${a.acknowledged ? "" : `<button class="button secondary small" data-ack="${esc(a.id)}">Acknowledge</button>`}</div>`).join("")
    : '<div class="empty-inline">No alerts since SentryCare started.</div>';
}

function renderAlertSettings(){
  const box = $("alert-settings"), s = alerting.settings; if(!box || !s) return;
  const last = s.last_push ? (s.last_push.ok ? `Last push sent ${new Date(s.last_push.time).toLocaleTimeString()}` : `Last push failed: ${s.last_push.error}`) : "No push sent yet";
  box.innerHTML = `<p class="subtle">Get alerts on phones even when the screen is locked, using the free <b>ntfy</b> app (Android/iOS). Off by default. Only short text is sent; never images.</p>
    <ol class="net-steps"><li>Install <b>ntfy</b> on each caretaker phone.</li><li>In ntfy tap <b>+</b> and subscribe to the topic below (default server ntfy.sh).</li><li>Turn on push, save, and press <b>Send test push</b>.</li></ol>
    <form id="ntfy-form" class="net-form">
      <label for="ntfy-topic">Topic (keep it secret, like a password)</label>
      <div class="input-action"><input id="ntfy-topic" value="${esc(s.ntfy_topic)}" placeholder="${esc(s.suggested_topic)}" autocomplete="off"><button type="button" class="button secondary small" id="ntfy-generate">Generate</button></div>
      <label for="ntfy-server">Server</label><input id="ntfy-server" value="${esc(s.ntfy_server)}">
      <label class="check"><input type="checkbox" id="ntfy-enabled" ${s.ntfy_enabled ? "checked" : ""}> Send push notifications for falls</label>
      <label class="check"><input type="checkbox" id="ntfy-person" ${s.include_person ? "checked" : ""}> Include the recognised person's name (sent to the ntfy server)</label>
      <div class="row-buttons"><button class="button primary" type="submit">Save</button><button class="button secondary" type="button" id="ntfy-test">Send test push</button></div>
      <p class="help-note">${esc(last)}</p></form>`;
}

async function loadAlertSettings(){
  try{ alerting.settings = await api("/api/alerts/settings"); renderAlertSettings(); }
  catch{ const box = $("alert-settings"); if(box) box.closest(".card").hidden = true; const sim = $("simulate-fall"); if(sim) sim.hidden = true; }
}

async function syncAlerts(){
  try{
    const data = await api("/api/alerts");
    alerting.history = data.alerts; renderAlertHistory();
    const recent = Date.now() / 1000 - 600;
    for(const a of data.alerts){ if(!a.acknowledged && a.epoch > recent) receive(a, false); else if(alerting.active.has(a.id)) alerting.active.set(a.id, a); }
    renderOverlay();
  }catch{}
}

function connectAlerts(){
  if(!window.EventSource) return;
  alerting.source = new EventSource("/api/alerts/stream");
  alerting.source.addEventListener("open", syncAlerts);
  alerting.source.addEventListener("alert", e => receive(JSON.parse(e.data), true));
  alerting.source.addEventListener("ack", e => { const a = JSON.parse(e.data); alerting.active.set(a.id, a); upsertHistory(a); renderOverlay(); });
}

// ---- events ----------------------------------------------------------------------------------
document.addEventListener("click", async event => {
  if(alerting.wantSound && (!alerting.audio || alerting.audio.state !== "running")){ unlockAudio(); setTimeout(() => { renderAlertToggle(); renderOverlay(); }, 100); }
  const ack = event.target.closest("[data-ack]");
  if(ack){
    ack.disabled = true;
    try{ const a = await api(`/api/alerts/${encodeURIComponent(ack.dataset.ack)}/ack`, {method:"POST"}); alerting.active.set(a.id, a); upsertHistory(a); renderOverlay(); }
    catch(error){ toast(error.message, true); ack.disabled = false; }
    return;
  }
  if(event.target.closest("#alerts-toggle")){ enableAlerts(); return; }
  if(event.target.closest("#simulate-fall")){
    try{ await api("/api/alerts/test", {method:"POST", json:{source_id: (typeof net !== "undefined" && net.selected) || null}}); }
    catch(error){ toast(error.message, true); }
    return;
  }
  if(event.target.closest("#ntfy-generate")){ $("ntfy-topic").value = alerting.settings.suggested_topic; return; }
  if(event.target.closest("#ntfy-test")){
    try{ await api("/api/alerts/settings/test-push", {method:"POST"}); toast("Test push sent. Check the ntfy app."); }
    catch(error){ toast(error.message, true); }
    loadAlertSettings();
  }
});
document.addEventListener("submit", async event => {
  if(event.target.id !== "ntfy-form") return;
  event.preventDefault();
  try{
    const saved = await api("/api/alerts/settings", {method:"POST", json:{ntfy_topic:$("ntfy-topic").value, ntfy_server:$("ntfy-server").value,
      ntfy_enabled:$("ntfy-enabled").checked, include_person:$("ntfy-person").checked}});
    alerting.settings = {...alerting.settings, ...saved}; renderAlertSettings(); toast("Alert settings saved");
  }catch(error){ toast(error.message, true); }
});

if(alerting.wantSound) registerWorker();
renderAlertToggle();
connectAlerts();
loadAlertSettings();
