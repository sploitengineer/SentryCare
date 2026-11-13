"use strict";
// Phone camera page: streams the newest JPEG frame to the SentryCare server, one upload in flight.
const $ = id => document.getElementById(id);
const cam = {stream:null, source:null, token:"", running:false, sent:0, started:0, wakeLock:null, failures:0};
const canvas = document.createElement("canvas");
const TARGET_FPS = 8;

try{ $("name").value = localStorage.getItem("sentrycare-camera-name") || ""; }catch{}

function say(text, error=false){ $("message").textContent = text; $("message").className = error ? "error" : ""; }

async function api(path, options={}){
  const headers = {"X-SentryCare-Token":cam.token, ...(options.headers||{})};
  if(options.json!==undefined){ options.body = JSON.stringify(options.json); headers["Content-Type"]="application/json"; delete options.json; }
  const response = await fetch(path, {...options, headers});
  if(response.status===401){ location.href="/login?next=camera"; throw new Error("Sign in again"); }
  const data = await response.json().catch(()=>({}));
  if(!response.ok){ const err = new Error(data.error||"Request failed"); err.status = response.status; throw err; }
  return data;
}

async function connectSource(){
  const session = await api("/api/session"); cam.token = session.token;
  const name = $("name").value.trim() || "Phone camera";
  try{ localStorage.setItem("sentrycare-camera-name", name); }catch{}
  cam.source = await api("/api/sources", {method:"POST", json:{kind:"browser", name}});
}

async function start(){
  if(!window.isSecureContext || !navigator.mediaDevices?.getUserMedia){
    say("Camera needs HTTPS. Open the https:// address shown on the SentryCare computer.", true); return;
  }
  $("start").disabled = true;
  try{
    // Ask for the sensor's native framing. A fixed landscape width/height makes portrait phones
    // crop the sensor ("crop-and-scale"), which looks zoomed in on the dashboard.
    cam.stream = await navigator.mediaDevices.getUserMedia({audio:false, video:{
      facingMode:{ideal:$("facing").value}, resizeMode:{ideal:"none"}, frameRate:{ideal:24}}});
    await widestView(cam.stream.getVideoTracks()[0]);
    $("video").srcObject = cam.stream;
    await connectSource();
    cam.running = true; cam.sent = 0; cam.started = performance.now(); cam.failures = 0;
    $("stop").disabled = false; $("name").disabled = true; $("facing").disabled = true;
    $("badge").textContent = "● LIVE · " + cam.source.name;
    say("Streaming to SentryCare. Keep this page open.");
    try{ cam.wakeLock = await navigator.wakeLock?.request("screen"); }catch{}
    loop();
  }catch(error){
    say(error.name==="NotAllowedError" ? "Camera permission was denied. Allow it in the browser settings." : error.message, true);
    stop(false);
  }
}

async function widestView(track){
  // Room monitoring needs the widest field of view: reset any digital zoom to its minimum.
  try{
    const zoom = track?.getCapabilities?.().zoom;
    if(zoom && zoom.min !== undefined) await track.applyConstraints({advanced:[{zoom:zoom.min}]});
  }catch{}
}

function frameBlob(){
  const video = $("video");
  // Scale the longest side to the chosen quality, keeping the full field of view and aspect ratio.
  const vw = video.videoWidth || 640, vh = video.videoHeight || 480;
  const scale = Math.min(1, Number($("quality").value) / Math.max(vw, vh));
  const width = Math.round(vw * scale), height = Math.round(vh * scale);
  canvas.width = width; canvas.height = height;
  canvas.getContext("2d").drawImage(video, 0, 0, width, height);
  return new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", .75));
}

async function loop(){
  while(cam.running){
    const begin = performance.now();
    try{
      if($("video").videoWidth){
        const blob = await frameBlob();
        const status = await api(`/api/sources/${cam.source.id}/frame`, {method:"POST", body:blob, headers:{"Content-Type":"image/jpeg"}});
        cam.sent++; cam.failures = 0; render(status);
      }
    }catch(error){
      cam.failures++;
      if(error.status===404 && cam.running){ try{ await connectSource(); $("badge").textContent = "● LIVE · " + cam.source.name; continue; }catch{} }
      say(`Connection problem: ${error.message}. Retrying…`, true);
      await new Promise(r => setTimeout(r, Math.min(5000, 500 * cam.failures)));
    }
    const wait = 1000 / TARGET_FPS - (performance.now() - begin);
    if(wait > 0) await new Promise(r => setTimeout(r, wait));
  }
}

function render(status){
  const seconds = (performance.now() - cam.started) / 1000;
  $("stats").textContent = `Sent ${cam.sent} frames · ${(cam.sent / Math.max(seconds, 1)).toFixed(1)} fps · server analysis ${status.analysis_fps} fps`;
  const box = $("state"); box.hidden = false;
  const people = status.faces.length ? status.faces.map(f => f.name).join(", ") : "No faces";
  if(status.fall_state === "CONFIRMED_FALL"){ box.className = "state fall"; box.textContent = "FALL DETECTED · " + people; }
  else if(status.fall_state === "POSSIBLE_FALL" || status.fall_state === "CHECKING_DOWN"){ box.className = "state check"; box.textContent = "Checking posture · " + people; }
  else { box.className = "state"; box.textContent = (status.pose_usable ? "Person visible" : "No body in view") + " · " + people; }
  if(status.error) say(status.error, true); else if($("message").className) say("Streaming to SentryCare. Keep this page open.");
}

async function stop(remove=true){
  cam.running = false;
  cam.stream?.getTracks().forEach(t => t.stop()); cam.stream = null; $("video").srcObject = null;
  try{ await cam.wakeLock?.release(); }catch{} cam.wakeLock = null;
  if(remove && cam.source){ try{ await api(`/api/sources/${cam.source.id}`, {method:"DELETE"}); }catch{} }
  cam.source = null;
  $("start").disabled = false; $("stop").disabled = true; $("name").disabled = false; $("facing").disabled = false;
  $("badge").textContent = "Camera off"; $("state").hidden = true;
}

$("start").addEventListener("click", start);
$("stop").addEventListener("click", () => { stop(); say("Stopped. The camera is off."); });
document.addEventListener("visibilitychange", async () => {
  if(document.visibilityState === "visible" && cam.running && !cam.wakeLock){ try{ cam.wakeLock = await navigator.wakeLock?.request("screen"); }catch{} }
});
window.addEventListener("pagehide", () => cam.stream?.getTracks().forEach(t => t.stop()));
