import tempfile
import time
import unittest

import cv2
import numpy as np

from sentrycare.sources import SourceManager, redact_url, validate_stream_url, annotate
from sentrycare.studio import Studio
from sentrycare.web import create_app


class FakeStudio:
    def __init__(self, busy=False, state="NORMAL"):
        self.calls, self.busy, self.state, self.dropped = [], busy, state, []
    def analyze(self, frame, session_id, mode="monitor", camera=None):
        self.calls.append((session_id, camera, frame.shape))
        if self.busy:
            return None
        return {"faces": [{"bbox": [10, 10, 40, 40], "name": "Unknown", "person_id": None}],
                "fall": {"state": self.state, "pose_usable": True, "landmarks": []},
                "width": frame.shape[1], "height": frame.shape[0], "processing_ms": 12}
    def drop_session(self, session_id):
        self.dropped.append(session_id)


class FakeCapture:
    def __init__(self, count):
        self.count, self.released = count, False
    def isOpened(self):
        return True
    def read(self):
        if self.count <= 0:
            return False, None
        self.count -= 1
        return True, np.full((120, 160, 3), 90, np.uint8)
    def release(self):
        self.released = True


FRAME = np.full((240, 320, 3), 100, np.uint8)
JPEG = cv2.imencode(".jpg", FRAME)[1].tobytes()


class SourceManagerTests(unittest.TestCase):
    def test_url_validation_and_redaction(self):
        self.assertEqual(validate_stream_url(" 0 "), "0")
        self.assertEqual(validate_stream_url("http://192.168.1.5:8080/video"), "http://192.168.1.5:8080/video")
        for bad in ("", "file:///etc/passwd", "javascript:alert(1)", "ftp://x/y"):
            with self.assertRaises(ValueError):
                validate_stream_url(bad)
        self.assertEqual(redact_url("rtsp://admin:secret@10.0.0.2:554/live?token=abc"), "rtsp://***@10.0.0.2:554/live")
        self.assertNotIn("secret", redact_url("rtsp://admin:secret@cam/x"))

    def test_browser_source_latest_frame_analysis(self):
        studio = FakeStudio()
        manager = SourceManager(studio, start_worker=False)
        source = manager.add_browser("  Living   room ")
        self.assertEqual(source.name, "Living room")
        manager.push(source.id, FRAME)
        manager.push(source.id, FRAME[:120, :160])     # only the newest frame is analysed
        self.assertEqual(manager.analyze_once(), 1)
        self.assertEqual(studio.calls, [(f"src{source.id}", "Living room", (120, 160, 3))])
        self.assertEqual(manager.analyze_once(), 0)    # nothing new
        public = manager.list()[0]
        self.assertTrue(public["online"])
        self.assertEqual(public["faces"], [{"name": "Unknown", "known": False}])
        jpeg = manager.snapshot(source.id)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertTrue(manager.remove(source.id))
        self.assertEqual(studio.dropped, [f"src{source.id}"])
        self.assertEqual(manager.list(), [])

    def test_busy_studio_retries_and_limits(self):
        studio = FakeStudio(busy=True)
        manager = SourceManager(studio, start_worker=False)
        source = manager.add_browser("A")
        manager.push(source.id, FRAME)
        self.assertEqual(manager.analyze_once(), 0)
        studio.busy = False
        self.assertEqual(manager.analyze_once(), 1)
        for name in "BCD":
            manager.add_browser(name)
        with self.assertRaises(ValueError):
            manager.add_browser("E")

    def test_stream_source_reads_and_releases(self):
        captures = []
        def factory(value):
            captures.append(FakeCapture(3))
            return captures[-1]
        manager = SourceManager(FakeStudio(), capture_factory=factory, start_worker=False)
        source = manager.add_stream("Hall", "http://192.168.1.9:8080/video")
        deadline = time.time() + 3
        while source.frame_seq < 3 and time.time() < deadline:
            time.sleep(.01)
        self.assertGreaterEqual(source.frame_seq, 3)
        with self.assertRaises(ValueError):
            manager.push(source.id, FRAME)
        manager.remove(source.id)
        source.thread.join(4)
        self.assertFalse(source.thread.is_alive())
        self.assertTrue(all(c.released for c in captures))

    def test_annotation_marks_fall(self):
        result = FakeStudio(state="CONFIRMED_FALL").analyze(FRAME, "x")
        image = annotate(FRAME, result, "Cam")
        self.assertEqual(image.shape, FRAME.shape)
        self.assertGreater(int(image[5, 160, 2]), 150)   # red banner at the top


class NetworkWebTests(unittest.TestCase):
    def make(self, lan=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        studio = Studio(temp.name, engine=object(), autostart=False)
        studio.status = "ready"
        self.fake = FakeStudio()
        manager = SourceManager(self.fake, start_worker=False)
        app = create_app(studio, lan=lan, sources=manager)
        return app.test_client(), manager

    def test_local_mode_source_api(self):
        client, manager = self.make()
        token = client.get("/api/state").json["token"]
        headers = {"X-SentryCare-Token": token}
        self.assertEqual(client.post("/api/sources", json={"kind": "browser"}).status_code, 403)
        source = client.post("/api/sources", json={"kind": "browser", "name": "Phone"}, headers=headers).json
        pushed = client.post(f"/api/sources/{source['id']}/frame", data=JPEG, content_type="image/jpeg", headers=headers)
        self.assertEqual(pushed.status_code, 200)
        manager.analyze_once()
        self.assertEqual(client.get("/api/sources").json["sources"][0]["fall_state"], "NORMAL")
        self.assertEqual(client.get(f"/api/sources/{source['id']}/snapshot.jpg").mimetype, "image/jpeg")
        self.assertEqual(client.post("/api/sources/missing/frame", data=JPEG, headers=headers).status_code, 404)
        self.assertEqual(client.post("/api/sources", json={"kind": "stream", "url": "file:///x"}, headers=headers).status_code, 400)
        self.assertEqual(client.delete(f"/api/sources/{source['id']}", headers=headers).status_code, 200)
        self.assertFalse(client.get("/api/network").json["lan"])
        self.assertEqual(client.get("/camera").status_code, 200)

    def test_lan_mode_requires_pin_for_remote_clients(self):
        lan = {"pin": "482913", "hosts": {"192.168.1.20"}, "urls": ["https://192.168.1.20:8765"], "secure": False}
        client, _ = self.make(lan)
        remote = {"REMOTE_ADDR": "192.168.1.50"}
        base = "https://192.168.1.20:8765"
        self.assertEqual(client.get("/api/state", base_url=base, environ_base=remote).status_code, 401)
        page = client.get("/camera", base_url=base, environ_base=remote)
        self.assertEqual(page.status_code, 302)
        self.assertIn("/login?next=camera", page.headers["Location"])
        # Unknown Host header is still rejected (DNS-rebinding guard).
        self.assertEqual(client.get("/login", base_url="https://evil.example", environ_base=remote).status_code, 403)
        bad = client.post("/login", data={"pin": "000000"}, base_url=base, environ_base=remote)
        self.assertEqual(bad.status_code, 401)
        good = client.post("/login?next=camera", data={"pin": "482913"}, base_url=base, environ_base=remote)
        self.assertEqual(good.status_code, 302)
        self.assertTrue(good.headers["Location"].endswith("/camera"))
        state = client.get("/api/state", base_url=base, environ_base=remote)
        self.assertEqual(state.status_code, 200)
        headers = {"X-SentryCare-Token": state.json["token"]}
        # Remote clients may stream a phone camera but not make the server open arbitrary URLs.
        self.assertEqual(client.post("/api/sources", json={"kind": "browser"}, headers=headers,
                                     base_url=base, environ_base=remote).status_code, 201)
        self.assertEqual(client.post("/api/sources", json={"kind": "stream", "url": "http://10.0.0.1/x"},
                                     headers=headers, base_url=base, environ_base=remote).status_code, 403)
        self.assertEqual(client.get("/api/network", base_url=base, environ_base=remote).json["camera_urls"],
                         ["https://192.168.1.20:8765/camera"])

    def test_lan_login_lockout(self):
        lan = {"pin": "482913", "hosts": {"192.168.1.20"}, "urls": [], "secure": False}
        client, _ = self.make(lan)
        kwargs = {"base_url": "https://192.168.1.20:8765", "environ_base": {"REMOTE_ADDR": "192.168.1.77"}}
        for _ in range(5):
            client.post("/login", data={"pin": "1"}, **kwargs)
        locked = client.post("/login", data={"pin": "482913"}, **kwargs)
        self.assertEqual(locked.status_code, 401)
        self.assertIn(b"Too many attempts", locked.data)


class CertificateTests(unittest.TestCase):
    def test_certificate_covers_addresses_and_is_reused(self):
        from sentrycare.netinfo import ensure_certificate
        with tempfile.TemporaryDirectory() as temp:
            cert, key = ensure_certificate(temp, ["192.168.1.20"])
            first = cert.read_bytes()
            self.assertEqual(ensure_certificate(temp, ["192.168.1.20"])[0].read_bytes(), first)
            self.assertNotEqual(ensure_certificate(temp, ["10.0.0.4"])[0].read_bytes(), first)
            self.assertIn(b"PRIVATE KEY", key.read_bytes())


if __name__ == "__main__":
    unittest.main()


class PhoneAccessToggleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        studio = Studio(temp.name, engine=object(), autostart=False)
        studio.status = "ready"
        from unittest import mock
        import sentrycare.lan
        patcher = mock.patch.object(sentrycare.lan, "DATA_DIR", __import__("pathlib").Path(temp.name))
        patcher.start(); self.addCleanup(patcher.stop)
        lanp = mock.patch("sentrycare.netinfo.lan_addresses", return_value=["192.168.1.20"])
        lanp.start(); self.addCleanup(lanp.stop)
        self.app = create_app(studio, sources=SourceManager(FakeStudio(), start_worker=False), phone_port=0)
        self.client = self.app.test_client()
        self.addCleanup(lambda: self.app.extensions["network"].stop())
        self.token = {"X-SentryCare-Token": self.client.get("/api/state").json["token"]}

    def test_enable_disable_from_local_only(self):
        remote = {"base_url": "https://192.168.1.20:8766", "environ_base": {"REMOTE_ADDR": "192.168.1.50"}}
        self.assertFalse(self.client.get("/api/network").json["enabled"])
        self.assertEqual(self.client.get("/ping", **remote).status_code, 403)       # off: LAN host refused
        on = self.client.post("/api/network/enable", headers=self.token)
        self.assertEqual(on.status_code, 200, on.json)
        info = on.json
        self.assertTrue(info["enabled"] and info["managed"])
        self.assertRegex(info["pin"], r"^\d{6}$")
        self.assertTrue(info["camera_urls"][0].startswith("https://192.168.1.20:"))
        self.assertTrue(info["qr"].startswith("data:image/svg+xml;base64,"))
        # Phone can reach the test page without a PIN, but not the API; it never sees the PIN.
        self.assertEqual(self.client.get("/ping", **remote).status_code, 200)
        self.assertEqual(self.client.get("/api/state", **remote).status_code, 401)
        self.assertEqual(self.client.post("/login", data={"pin": info["pin"]}, **remote).status_code, 302)
        public = self.client.get("/api/network", **remote).json
        self.assertNotIn("pin", public)
        self.assertNotIn("qr", public)
        self.assertEqual(self.client.post("/api/network/disable", headers=self.token, **remote).status_code, 403)
        # Turning off stops the listener and refuses the LAN host again.
        off = self.client.post("/api/network/disable", headers=self.token)
        self.assertFalse(off.json["enabled"])
        self.assertEqual(self.client.get("/ping", **remote).status_code, 403)

    def test_listener_really_serves_https(self):
        import ssl, urllib.request
        info = self.client.post("/api/network/enable", headers=self.token).json
        port = self.app.extensions["network"].server.server_port
        context = ssl._create_unverified_context()
        body = urllib.request.urlopen(f"https://127.0.0.1:{port}/ping", context=context, timeout=30).read()
        self.assertIn(b"Connected", body)
        self.client.post("/api/network/disable", headers=self.token)
