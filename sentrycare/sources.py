"""Server-owned camera sources for the camera network view.

Two kinds of source feed the same analysis loop:

* ``browser`` - a phone or laptop browser posts JPEG frames (the /camera page).
* ``stream``  - the server pulls a URL with OpenCV: an Android "IP Webcam"-style MJPEG URL,
  an RTSP camera, or a local webcam index such as ``0``.

Only the newest frame of each source is kept. One background worker analyses the newest
unprocessed frame of each source in turn, so slow inference drops stale frames instead of
queueing them. Viewers receive the newest raw frame with the newest analysis drawn on top,
which keeps the dashboard video smooth even when analysis runs slower than capture.
"""

import logging
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import cv2

from .video_stream import frames as video_frames

logger = logging.getLogger("SentryCare")

MAX_SOURCES = 4
OFFLINE_AFTER = 5.0
BROWSER_EXPIRY = 600.0
POSE_LINES = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24), (23, 24),
              (23, 25), (25, 27), (24, 26), (26, 28)]
STATE_LABEL = {"NORMAL": "Normal", "POSSIBLE_FALL": "Check posture", "CHECKING_DOWN": "Possible fall - confirming", "LYING": "Lying (no fall seen)",
               "CONFIRMED_FALL": "FALL DETECTED"}
COCO_LINES = [(5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15),
              (12, 14), (14, 16)]
# BGR colours per state
STATE_COLOR = {"NORMAL": (195, 213, 162), "POSSIBLE_FALL": (60, 190, 245), "CHECKING_DOWN": (30, 140, 240), "LYING": (200, 170, 120),
               "CONFIRMED_FALL": (60, 60, 230)}


def redact_url(url):
    """Hide credentials in a stream URL before it is shown or logged."""
    if url.isdigit():
        return f"Local webcam {url}"
    parts = urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, "***@" + host, parts.path, "", ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def validate_stream_url(url):
    url = (url or "").strip()
    if re.fullmatch(r"\d{1,2}", url):
        return url
    if not re.match(r"^(https?|rtsps?)://[^\s]+$", url, re.IGNORECASE) or len(url) > 500:
        raise ValueError("Enter an http(s)://, rtsp:// URL or a webcam number such as 0")
    return url


def clean_name(name, fallback):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:40]
    return name or fallback


class CameraSource:
    def __init__(self, kind, name, url=None):
        self.id = uuid4().hex[:12]
        self.kind, self.name, self.url = kind, name, url
        self.created = time.time()
        self.created_monotonic = time.monotonic()
        self.cond = threading.Condition()
        self.frame = None          # newest raw BGR frame
        self.frame_seq = 0
        self.frame_time = 0.0
        self.analyzed_seq = 0
        self.result = None         # newest analysis result (dict)
        self.error = None
        self.stop = threading.Event()
        self.thread = None
        self.fps_window = []
        self.analysis_fps_window = []

    def put(self, frame):
        now = time.monotonic()
        with self.cond:
            self.frame = frame
            self.frame_seq += 1
            self.frame_time = now
            self.fps_window = [t for t in self.fps_window if now - t < 2] + [now]
            self.cond.notify_all()

    def online(self):
        return self.frame is not None and time.monotonic() - self.frame_time < OFFLINE_AFTER

    def public(self):
        result = self.result or {}
        fall = result.get("fall") or {}
        return {
            "id": self.id, "kind": self.kind, "name": self.name,
            "url": redact_url(self.url) if self.url else None,
            "online": self.online(), "error": self.error,
            "capture_fps": round(len(self.fps_window) / 2, 1) if self.online() else 0,
            "analysis_fps": round(len(self.analysis_fps_window) / 2, 1) if self.online() else 0,
            "fall_state": fall.get("state", "NORMAL") if self.online() else None,
            "pose_usable": bool(fall.get("pose_usable")),
            "people": fall.get("people", 0) if self.online() else 0,
            "faces": [{"name": f.get("name"), "known": bool(f.get("person_id"))} for f in result.get("faces", [])]
                     if self.online() else [],
            "processing_ms": result.get("processing_ms"),
        }


class SourceManager:
    def __init__(self, studio, capture_factory=None, start_worker=True):
        self.studio = studio
        self.sources = {}
        self.lock = threading.Lock()
        self.capture_factory = capture_factory
        self.closed = threading.Event()
        self.worker = None
        self.start_worker = start_worker
        self.on_alert = None   # callable(camera_name, frame, result) for newly confirmed falls

    def _ensure_worker(self):
        if self.start_worker and self.worker is None:
            self.worker = threading.Thread(target=self._analysis_loop, daemon=True, name="source-analysis")
            self.worker.start()

    # ---- lifecycle -------------------------------------------------------------------------
    def list(self):
        # Forget phone cameras whose page was closed long ago; stream cameras keep retrying.
        stale = [s.id for s in self.sources.values() if s.kind == "browser"
                 and time.monotonic() - (s.frame_time or s.created_monotonic) > BROWSER_EXPIRY]
        for source_id in stale:
            self.remove(source_id)
        with self.lock:
            return [s.public() for s in sorted(self.sources.values(), key=lambda s: s.created)]

    def get(self, source_id):
        source = self.sources.get(source_id)
        if source is None:
            raise KeyError(source_id)
        return source

    def _add(self, source):
        with self.lock:
            if len(self.sources) >= MAX_SOURCES:
                raise ValueError(f"At most {MAX_SOURCES} cameras can be connected; remove one first")
            self.sources[source.id] = source
            self._ensure_worker()
        return source

    def add_browser(self, name):
        return self._add(CameraSource("browser", clean_name(name, "Phone camera")))

    def add_stream(self, name, url):
        url = validate_stream_url(url)
        source = self._add(CameraSource("stream", clean_name(name, "Network camera"), url))
        source.thread = threading.Thread(target=self._reader, args=(source,), daemon=True,
                                         name=f"source-reader-{source.id}")
        source.thread.start()
        return source

    def remove(self, source_id):
        with self.lock:
            source = self.sources.pop(source_id, None)
        if source is None:
            return False
        source.stop.set()
        with source.cond:
            source.cond.notify_all()
        self.studio.drop_session(f"src{source.id}")
        return True

    def close(self):
        self.closed.set()
        for source_id in list(self.sources):
            self.remove(source_id)

    # ---- input -----------------------------------------------------------------------------
    def push(self, source_id, frame):
        source = self.get(source_id)
        if source.kind != "browser":
            raise ValueError("This camera is pulled by the server and does not accept uploads")
        source.put(frame)
        return source.public()

    def _reader(self, source):
        source_value = int(source.url) if source.url.isdigit() else source.url
        while not source.stop.is_set():
            try:
                stream = video_frames(source_value, max_reconnect=2, retry_delay=1.0,
                                      capture_factory=self.capture_factory)
                try:
                    for frame, _ in stream:
                        if source.stop.is_set():
                            break
                        source.error = None
                        source.put(frame)
                finally:
                    stream.close()
                if not source.stop.is_set():
                    source.error = "Stream ended; reconnecting"
            except Exception as exc:  # keep retrying; the dashboard shows the reason
                source.error = f"Cannot read stream: {exc}"
                logger.warning("Camera %s: %s", source.name, exc)
            source.stop.wait(2.0)

    # ---- analysis --------------------------------------------------------------------------
    def analyze_once(self):
        """Analyse the newest pending frame of each source once. Returns how many were analysed."""
        done = 0
        for source in list(self.sources.values()):
            with source.cond:
                if source.frame is None or source.frame_seq == source.analyzed_seq:
                    continue
                frame, seq = source.frame, source.frame_seq
            try:
                result = self.studio.analyze(frame, f"src{source.id}", "monitor", camera=source.name)
            except RuntimeError as exc:   # models still loading or failed
                source.error = str(exc)
                source.analyzed_seq = seq
                continue
            except Exception as exc:
                logger.exception("Analysis failed for camera %s", source.name)
                source.error = f"Analysis failed: {exc}"
                source.analyzed_seq = seq
                continue
            if result is None:   # another analysis is running; retry later with a newer frame
                continue
            now = time.monotonic()
            with source.cond:
                source.result, source.analyzed_seq = result, seq
                if source.error and source.error.startswith(("Analysis", "Models")):
                    source.error = None
                source.analysis_fps_window = [t for t in source.analysis_fps_window if now - t < 2] + [now]
                source.cond.notify_all()
            for alert in result.get("alerts") or []:
                if not self.on_alert:
                    break
                try:
                    self.on_alert(source.name, frame, result, alert)
                except Exception:
                    logger.exception("Could not publish alert for camera %s", source.name)
            done += 1
        return done

    def _analysis_loop(self):
        while not self.closed.is_set():
            try:
                if not self.analyze_once():
                    time.sleep(.02)
            except Exception:
                logger.exception("Camera analysis loop error")
                time.sleep(.5)

    # ---- output ----------------------------------------------------------------------------
    def latest(self, source_id):
        source = self.get(source_id)
        with source.cond:
            return source.frame, source.result

    def snapshot(self, source_id, max_width=960, quality=78):
        source = self.get(source_id)
        with source.cond:
            frame, result = source.frame, source.result
        if frame is None:
            return None
        image = annotate(frame, result, source.name)
        if image.shape[1] > max_width:
            scale = max_width / image.shape[1]
            image = cv2.resize(image, (max_width, round(image.shape[0] * scale)))
        ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return data.tobytes() if ok else None

    def mjpeg(self, source_id, max_fps=15):
        """Yield multipart MJPEG chunks of the annotated stream until the source is removed."""
        source = self.get(source_id)
        last_key, interval = None, 1.0 / max_fps
        while not source.stop.is_set() and not self.closed.is_set():
            with source.cond:
                source.cond.wait_for(lambda: (source.frame_seq, source.analyzed_seq) != last_key
                                     or source.stop.is_set(), timeout=5)
                key = (source.frame_seq, source.analyzed_seq)
            if source.stop.is_set():
                return
            if key == last_key:
                # Keep the connection alive while the camera is offline.
                data = offline_frame(source.name)
            else:
                data = self.snapshot(source_id)
                last_key = key
            if data:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode()
                       + b"\r\n\r\n" + data + b"\r\n")
            time.sleep(interval)


def annotate(frame, result, camera_name):
    image = frame.copy()
    height, width = image.shape[:2]
    thick = max(2, round(width / 480))
    font_scale = max(.5, width / 1400)
    if result:
        sx, sy = width / result["width"], height / result["height"]
        fall = result.get("fall") or {}
        state = fall.get("state", "NORMAL")
        points = fall.get("landmarks") or []
        bone = STATE_COLOR.get(state, STATE_COLOR["NORMAL"])
        for person in result.get("people") or []:
            color = STATE_COLOR.get(person["state"], STATE_COLOR["NORMAL"])
            kp = person.get("keypoints") or []
            for a, b in COCO_LINES:
                if a < len(kp) and b < len(kp) and kp[a][2] >= .5 and kp[b][2] >= .5:
                    cv2.line(image, (round(kp[a][0] * width), round(kp[a][1] * height)),
                             (round(kp[b][0] * width), round(kp[b][1] * height)), color, thick, cv2.LINE_AA)
            x1, y1, x2, y2 = person["box"]
            p1, p2 = (round(x1 * width), round(y1 * height)), (round(x2 * width), round(y2 * height))
            cv2.rectangle(image, p1, p2, color, max(1, thick - 1), cv2.LINE_AA)
            label = f"#{person['track_id']} {person.get('name') or ''}".strip()
            if person["state"] != "NORMAL":
                label += " - " + STATE_LABEL.get(person["state"], person["state"])
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale * .9, 1)
            ty = min(height - 4, p2[1] + th + 10) if p1[1] < th + 12 else p1[1]
            cv2.rectangle(image, (p1[0], ty - th - 10), (p1[0] + tw + 12, ty), color, -1)
            cv2.putText(image, label, (p1[0] + 6, ty - 6), cv2.FONT_HERSHEY_SIMPLEX, font_scale * .9, (20, 20, 20), 1,
                        cv2.LINE_AA)
        for a, b in POSE_LINES:
            if a < len(points) and b < len(points) and points[a][2] >= .5 and points[b][2] >= .5:
                cv2.line(image, (round(points[a][0] * width), round(points[a][1] * height)),
                         (round(points[b][0] * width), round(points[b][1] * height)), bone, thick, cv2.LINE_AA)
        for face in result.get("faces", []):
            x, y, w, h = face["bbox"]
            x, y, w, h = round(x * sx), round(y * sy), round(w * sx), round(h * sy)
            color = (174, 233, 176) if face.get("person_id") else (147, 211, 239) if face.get("name") == "Unknown" else (225, 215, 184)
            cv2.rectangle(image, (x, y), (x + w, y + h), color, thick, cv2.LINE_AA)
            label = str(face.get("name") or "")
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)
            top = max(th + 10, y)
            cv2.rectangle(image, (x, top - th - 10), (x + tw + 12, top), (45, 55, 19), -1)
            cv2.putText(image, label, (x + 6, top - 6), cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, 1, cv2.LINE_AA)
        if state != "NORMAL":
            banner = (40, 40, 200) if state == "CONFIRMED_FALL" else (30, 120, 220) if state == "CHECKING_DOWN" else (30, 140, 210)
            bar = round(44 * font_scale / .5 * .6) + 16
            cv2.rectangle(image, (0, 0), (width, bar), banner, -1)
            cv2.putText(image, STATE_LABEL.get(state, state), (14, bar - 14), cv2.FONT_HERSHEY_SIMPLEX,
                        font_scale * 1.4, (255, 255, 255), thick, cv2.LINE_AA)
    label = camera_name
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)
    cv2.rectangle(image, (0, height - th - 14), (tw + 16, height), (20, 20, 20), -1)
    cv2.putText(image, label, (8, height - 8), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (240, 240, 240), 1, cv2.LINE_AA)
    return image


_offline_cache = {}


def offline_frame(name):
    if name not in _offline_cache:
        import numpy as np
        image = np.full((360, 640, 3), 32, np.uint8)
        cv2.putText(image, f"{name}: waiting for video", (30, 190), cv2.FONT_HERSHEY_SIMPLEX, .8,
                    (200, 200, 200), 2, cv2.LINE_AA)
        _offline_cache[name] = cv2.imencode(".jpg", image)[1].tobytes()
    return _offline_cache[name]
