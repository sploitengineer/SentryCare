"""Fall alerts: live broadcast to every open dashboard, acknowledgement, and optional ntfy push.

* Alerts and their snapshots live in memory only (the newest 50). Event history in SQLite keeps
  the durable record without images.
* Dashboards subscribe with Server-Sent Events (``/api/alerts/stream``); an acknowledgement on any
  screen is broadcast so every screen stops its siren.
* ntfy push is off by default. When enabled from this computer it sends a short text alert to the
  configured topic. Snapshots are never sent; the person's name only when explicitly allowed.
"""

from collections import deque
import json
import logging
from pathlib import Path
import queue
import re
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timezone

import cv2

logger = logging.getLogger("SentryCare")

DEFAULT_SETTINGS = {"ntfy_enabled": False, "ntfy_server": "https://ntfy.sh", "ntfy_topic": "",
                    "include_person": False}
MAX_SUBSCRIBERS = 24
KIND_TITLE = {"FALL": "Fall detected", "TEST": "Test alert (simulated fall)"}


def validate_settings(data, current):
    settings = dict(current)
    if "ntfy_enabled" in data:
        settings["ntfy_enabled"] = bool(data["ntfy_enabled"])
    if "include_person" in data:
        settings["include_person"] = bool(data["include_person"])
    if "ntfy_server" in data:
        server = str(data["ntfy_server"] or "").strip().rstrip("/") or DEFAULT_SETTINGS["ntfy_server"]
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._\-/]*)?", server):
            raise ValueError("Enter the ntfy server as https://ntfy.sh or your own server URL")
        settings["ntfy_server"] = server
    if "ntfy_topic" in data:
        topic = str(data["ntfy_topic"] or "").strip()
        if topic and not re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", topic):
            raise ValueError("The topic must be 8-64 letters, numbers, - or _ (it acts like a password)")
        settings["ntfy_topic"] = topic
    if settings["ntfy_enabled"] and not settings["ntfy_topic"]:
        raise ValueError("Choose a topic before turning on phone push")
    return settings


def suggest_topic():
    return "sentrycare-" + secrets.token_hex(6)


class AlertHub:
    def __init__(self, settings_path=None, sender=None, click_url=None):
        self.alerts = deque(maxlen=50)
        self.snapshots = {}
        self.lock = threading.Lock()
        self.subscribers = set()
        self.settings_path = Path(settings_path) if settings_path else None
        self.settings = dict(DEFAULT_SETTINGS)
        self.sender = sender or self._http_post      # replaced in tests: no network
        self.click_url = click_url or (lambda: None)  # dashboard link for the push notification
        self.last_push = None
        if self.settings_path and self.settings_path.is_file():
            try:
                self.settings = validate_settings(json.loads(self.settings_path.read_text(encoding="utf-8")),
                                                  DEFAULT_SETTINGS)
            except Exception:
                logger.warning("Ignoring unreadable alert settings")

    # ---- alerts ----------------------------------------------------------------------------
    def publish(self, kind, camera, person, details=None, frame=None, result=None):
        alert_id = secrets.token_hex(8)
        now = datetime.now(timezone.utc)
        alert = {"id": alert_id, "kind": kind, "title": KIND_TITLE.get(kind, kind),
                 "camera": camera or "Camera", "person": person or "Unassigned person",
                 "time": now.isoformat(timespec="seconds"), "epoch": now.timestamp(),
                 "details": details or {}, "snapshot": False, "acknowledged": None}
        if frame is not None:
            from .sources import annotate
            image = annotate(frame, result, alert["camera"])
            if image.shape[1] > 800:
                scale = 800 / image.shape[1]
                image = cv2.resize(image, (800, round(image.shape[0] * scale)))
            ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                alert["snapshot"] = True
                self.snapshots[alert_id] = data.tobytes()
        with self.lock:
            if len(self.alerts) == self.alerts.maxlen:
                self.snapshots.pop(self.alerts[0]["id"], None)
            self.alerts.append(alert)
        self._broadcast("alert", alert)
        if self.settings["ntfy_enabled"]:
            threading.Thread(target=self._push, args=(alert,), daemon=True, name="ntfy-push").start()
        return alert

    def list(self):
        with self.lock:
            return list(reversed(self.alerts))

    def snapshot(self, alert_id):
        return self.snapshots.get(alert_id)

    def acknowledge(self, alert_id, by):
        with self.lock:
            alert = next((a for a in self.alerts if a["id"] == alert_id), None)
            if alert is None:
                return None
            if not alert["acknowledged"]:
                alert["acknowledged"] = {"by": by, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._broadcast("ack", alert)
        return alert

    # ---- live stream -----------------------------------------------------------------------
    def subscribe(self):
        with self.lock:
            if len(self.subscribers) >= MAX_SUBSCRIBERS:
                raise ValueError("Too many open dashboards; close some tabs")
            q = queue.Queue(maxsize=100)
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)

    def _broadcast(self, event, data):
        message = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self.lock:
            subscribers = list(self.subscribers)
        for q in subscribers:
            try:
                q.put_nowait(message)
            except queue.Full:   # a stuck client must not block alerts for everyone else
                pass

    def stream(self, q, heartbeat=15.0):
        # Yield bytes: the response uses direct_passthrough, so WSGI receives these unchanged.
        try:
            yield b"retry: 3000\n\n"
            while True:
                try:
                    yield q.get(timeout=heartbeat).encode("utf-8")
                except queue.Empty:
                    yield b": keep-alive\n\n"
        finally:
            self.unsubscribe(q)

    # ---- settings and ntfy -----------------------------------------------------------------
    def public_settings(self):
        return {**self.settings, "last_push": self.last_push}

    def update_settings(self, data):
        self.settings = validate_settings(data, self.settings)
        if self.settings_path:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        return self.public_settings()

    def push_message(self, alert):
        local = datetime.fromtimestamp(alert["epoch"]).strftime("%H:%M:%S")
        who = alert["person"] if self.settings["include_person"] else "A person"
        body = f"{who} may have fallen - {alert['camera']} at {local}. Check on them now."
        if alert["kind"] == "TEST":
            body = f"Test alert from {alert['camera']} at {local}. SentryCare push works."
        return alert["title"], body

    def _push(self, alert):
        title, body = self.push_message(alert)
        url = f"{self.settings['ntfy_server']}/{self.settings['ntfy_topic']}"
        headers = {"Title": title, "Priority": "urgent" if alert["kind"] == "FALL" else "high",
                   "Tags": "rotating_light,warning"}
        click = self.click_url()
        if click:
            headers["Click"] = click
        try:
            self.sender(url, body.encode("utf-8"), headers)
            self.last_push = {"ok": True, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        except Exception as exc:
            logger.warning("ntfy push failed: %s", exc)
            self.last_push = {"ok": False, "error": str(exc)[:200],
                              "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        return self.last_push

    @staticmethod
    def _http_post(url, body, headers):
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={**headers, "User-Agent": "SentryCare-demo"})
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status >= 300:
                raise RuntimeError(f"ntfy answered HTTP {response.status}")
