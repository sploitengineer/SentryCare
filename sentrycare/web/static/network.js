"use strict";
// Camera network tab: lists server-side camera sources and shows the selected one's annotated stream.
const net = {sources:[], selected:"", network:null, busy:false, streamFor:""};

function netLabel(source){
  if(!source.online) return source.error ? "Error" : "Offline";
  return {CONFIRMED_FALL:"Fall detected", CHECKING_DOWN:"Confirming fall", POSSIBLE_FALL:"Check posture"}[source.fall_state] || "Live";
}

function renderNetwork(){
  const tabs = $("net-tabs"), list = $("net-list");
  $("nav-cameras").textContent = net.sources.filter(s => s.online).length;
  tabs.innerHTML = net.sources.map(s => `<button class="net-tab${s.id===net.selected?" active":""}" data-source="${esc(s.id)}" role="tab"><span class="dot${s.fall_state==="CONFIRMED_FALL"?" fall":s.online?" on":""}"></span>${esc(s.name)}</button>`).join("");
  list.innerHTML = net.sources.length ? net.sources.map(s => `<div class="net-item${s.fall_state==="CONFIRMED_FALL"?" net-alert":""}"><div><strong>${esc(s.name)}</strong><small>${s.kind==="browser"?"Phone browser":esc(s.url)} · ${esc(netLabel(s))}${s.online?` · ${s.capture_fps} fps in · ${s.analysis_fps} analysed`:""}</small>${s.error?`<small>${esc(s.error)}</small>`:""}</div><button class="button secondary small" data-remove-source="${esc(s.id)}">Remove</button></div>`).join("")
    : '<div class="empty-inline">No cameras yet.</div>';

  const source = net.sources.find(s => s.id === net.selected);
  const img = $("net-video");
  if(!source){
    img.hidden = true; img.removeAttribute("src"); net.streamFor = "";
    $("net-empty").hidden = false; $("net-view-title").textContent = "No camera selected";
    $("net-view-sub").textContent = "Connect a phone or add a stream, then choose it here";
    $("net-view-badge").className = "pill neutral"; $("net-view-badge").innerHTML = "<i></i>Offline";
    $("net-people").textContent = "—"; $("net-latency").textContent = "Analysis —";
    return;
  }
  if(net.streamFor !== source.id){ img.src = `/api/sources/${encodeURIComponent(source.id)}/stream`; net.streamFor = source.id; }
  img.hidden = false; $("net-empty").hidden = true;
  $("net-view-title").textContent = source.name;
  $("net-view-sub").textContent = source.kind === "browser" ? "Phone camera over Wi-Fi" : source.url;
  const fall = source.fall_state === "CONFIRMED_FALL", check = ["POSSIBLE_FALL","CHECKING_DOWN"].includes(source.fall_state);
  $("net-view-badge").className = "pill " + (fall ? "red" : check ? "amber" : source.online ? "" : "neutral");
  $("net-view-badge").innerHTML = `<i></i>${esc(netLabel(source))}`;
  $("net-people").textContent = source.faces.length ? source.faces.map(f => f.name).join(", ") : (source.online ? (source.pose_usable ? "Person visible · no face" : "No one in view") : "Waiting for video");
  $("net-latency").textContent = source.processing_ms != null ? `Analysis ${source.processing_ms} ms · ${source.analysis_fps} fps` : "Analysis —";
}

function renderConnect(){
  const info = net.network, box = $("net-connect");
  if(!info) return;
  const key = JSON.stringify(info);
  if(box.dataset.key === key) return;   // avoid re-rendering (and flickering the QR) every poll
  box.dataset.key = key;
  if(!info.local){
    box.innerHTML = info.enabled ? `<p class="subtle">Phone camera page:</p>${info.camera_urls.map(u=>`<span class="net-link">${esc(u)}</span>`).join("")}` : '<p class="subtle">Phone access is off.</p>';
    return;
  }
  if(!info.enabled){
    box.innerHTML = `<p class="subtle">Use a phone on the same Wi-Fi or hotspot as a live camera. This starts a secure (HTTPS) link with a one-time PIN.</p>
      ${info.error?`<p class="net-error">${esc(info.error)}</p>`:""}
      <button class="button primary full" id="net-enable">Turn on phone access</button>
      <p class="help-note">Only devices with the PIN can connect. Turn it off when you are done.</p>`;
    return;
  }
  const [primary, ...others] = info.camera_urls;
  const ping = info.ping_urls[0];
  box.innerHTML = `
    <div class="net-on"><span class="pill"><i></i>Phone access on</span>${info.managed?'<button class="button secondary small" id="net-disable">Turn off</button>':""}</div>
    ${info.qr?`<img class="net-qr" src="${info.qr}" alt="QR code for the phone camera link">`:""}
    <div class="net-pin"><small>PIN</small><strong>${esc(info.pin)}</strong></div>
    <ol class="net-steps">
      <li>Connect the phone to the <b>same Wi-Fi or hotspot</b> as this laptop.</li>
      <li><b>Scan the QR code</b> with the phone camera, or type:<span class="net-link">${esc(primary)}</span></li>
      <li>On “Your connection is not private”, tap <b>Advanced → Proceed</b>. This is expected: the certificate is made on this laptop.</li>
      <li>Enter the PIN, then tap <b>Start streaming</b> and allow the camera.</li>
      <li>Select the phone at the top of this page.</li>
    </ol>
    <details class="net-help"><summary>Phone can’t open the link?</summary>
      <p>First open this test link on the phone: <span class="net-link">${esc(ping)}</span>
      If it shows <b>Connected</b>, the network is fine; retry the camera link. If it doesn’t load:</p>
      <ul><li>Type <b>https://</b> and the port <b>:${esc(info.port||"")}</b> exactly.</li>
      <li>Turn off VPN, ad blockers and “Private DNS” on the phone.</li>
      <li>If this laptop uses the phone’s own hotspot, some phones block that. Try another phone, or a router/Wi-Fi both can join.</li>
      <li>When Windows asks about Python and the firewall, choose <b>Allow</b>.</li></ul>
      ${others.length?`<p>Other addresses of this laptop (try if the first fails):</p>${others.map(u=>`<span class="net-link">${esc(u)}</span>`).join("")}`:""}
    </details>`;
}

async function setPhoneAccess(on){
  const button = $(on ? "net-enable" : "net-disable");
  if(button){ button.disabled = true; button.textContent = on ? "Starting…" : "Turning off…"; }
  try{ net.network = await api(on ? "/api/network/enable" : "/api/network/disable", {method:"POST"}); toast(on ? "Phone access is on" : "Phone access is off"); }
  catch(error){ toast(error.message, true); net.network = await api("/api/network").catch(()=>net.network); }
  renderConnect();
}

async function pollNetwork(){
  if(net.busy || $("tab-cameras").hidden) return;
  net.busy = true;
  try{
    net.network = await api("/api/network"); renderConnect();
    net.sources = (await api("/api/sources")).sources;
    if(!net.sources.some(s => s.id === net.selected)) net.selected = (net.sources.find(s => s.online) || net.sources[0] || {}).id || "";
    renderNetwork();
  }catch(error){ /* the main poller reports server problems */ }
  finally{ net.busy = false; }
}

document.addEventListener("click", async event => {
  if(event.target.closest("#net-enable")){ setPhoneAccess(true); return; }
  if(event.target.closest("#net-disable")){ setPhoneAccess(false); return; }
  const tab = event.target.closest("[data-source]");
  if(tab){ net.selected = tab.dataset.source; renderNetwork(); return; }
  const remove = event.target.closest("[data-remove-source]");
  if(remove){
    try{ await api(`/api/sources/${encodeURIComponent(remove.dataset.removeSource)}`, {method:"DELETE"}); toast("Camera removed"); }
    catch(error){ toast(error.message, true); }
    pollNetwork();
  }
});
$("stream-form").addEventListener("submit", async event => {
  event.preventDefault();
  try{
    const source = await api("/api/sources", {method:"POST", json:{kind:"stream", name:$("stream-name").value, url:$("stream-url").value}});
    net.selected = source.id; $("stream-url").value = ""; $("stream-name").value = "";
    toast("Camera added. Video appears when the stream connects.");
  }catch(error){ toast(error.message, true); }
  pollNetwork();
});
// Stop pulling MJPEG when the tab is hidden so idle dashboards do not use bandwidth.
setInterval(() => {
  if($("tab-cameras").hidden && net.streamFor){ $("net-video").removeAttribute("src"); net.streamFor = ""; }
  pollNetwork();
}, 1500);
