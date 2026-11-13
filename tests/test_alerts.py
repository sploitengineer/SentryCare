import json
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from sentrycare.alerts import AlertHub, validate_settings, DEFAULT_SETTINGS
from sentrycare.sources import SourceManager
from sentrycare.studio import Studio
from sentrycare.web import create_app

FRAME = np.full((240, 320, 3), 90, np.uint8)


class RecordingSender:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail
    def __call__(self, url, body, headers):
        self.calls.append((url, body.decode(), headers))
        if self.fail:
            raise OSError("network unreachable")


def wait_for(predicate, timeout=3):
    end = time.time() + timeout
    while time.time() < end and not predicate():
        time.sleep(.01)
    return predicate()


class AlertHubTests(unittest.TestCase):
    def test_publish_broadcast_ack_and_snapshot(self):
        hub = AlertHub(sender=RecordingSender())
        q = hub.subscribe()
        alert = hub.publish("FALL", "Living room", "Alice", {"score": 1.4}, FRAME,
                            {"faces": [], "fall": {"state": "CONFIRMED_FALL"}, "width": 320, "height": 240})
        self.assertTrue(alert["snapshot"])
        self.assertTrue(hub.snapshot(alert["id"]).startswith(b"\xff\xd8"))
        message = q.get_nowait()
        self.assertTrue(message.startswith("event: alert\n"))
        self.assertEqual(json.loads(message.split("data: ", 1)[1])["camera"], "Living room")
        acked = hub.acknowledge(alert["id"], "This computer")
        self.assertEqual(acked["acknowledged"]["by"], "This computer")
        self.assertTrue(q.get_nowait().startswith("event: ack\n"))
        first = acked["acknowledged"]["time"]
        self.assertEqual(hub.acknowledge(alert["id"], "Other")["acknowledged"]["by"], "This computer")
        self.assertEqual(first, hub.list()[0]["acknowledged"]["time"])
        self.assertIsNone(hub.acknowledge("missing", "x"))
        hub.unsubscribe(q)

    def test_history_is_bounded_and_drops_old_snapshots(self):
        hub = AlertHub(sender=RecordingSender())
        first = hub.publish("FALL", "Cam", None, frame=FRAME)
        for _ in range(55):
            hub.publish("FALL", "Cam", None, frame=FRAME)
        self.assertEqual(len(hub.list()), 50)
        self.assertIsNone(hub.snapshot(first["id"]))
        self.assertEqual(len(hub.snapshots), 50)

    def test_stuck_subscriber_does_not_block(self):
        hub = AlertHub(sender=RecordingSender())
        q = hub.subscribe()
        for _ in range(150):
            hub.publish("FALL", "Cam", None)
        self.assertEqual(q.qsize(), 100)

    def test_ntfy_off_by_default_and_private_text(self):
        sender = RecordingSender()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "alert_settings.json"
            hub = AlertHub(path, sender=sender, click_url=lambda: "https://192.168.1.20:8766/#events")
            hub.publish("FALL", "Hall", "Alice")
            time.sleep(.1)
            self.assertEqual(sender.calls, [])                     # nothing leaves the computer by default
            hub.update_settings({"ntfy_enabled": True, "ntfy_topic": "sentrycare-abc123"})
            self.assertTrue(path.is_file())
            hub.publish("FALL", "Hall", "Alice", frame=FRAME)
            self.assertTrue(wait_for(lambda: sender.calls))
            url, body, headers = sender.calls[0]
            self.assertEqual(url, "https://ntfy.sh/sentrycare-abc123")
            self.assertNotIn("Alice", body)                       # name only when allowed
            self.assertIn("Hall", body)
            self.assertEqual(headers["Priority"], "urgent")
            self.assertEqual(headers["Click"], "https://192.168.1.20:8766/#events")
            hub.update_settings({"include_person": True})
            hub.publish("FALL", "Hall", "Alice")
            self.assertTrue(wait_for(lambda: len(sender.calls) == 2))
            self.assertIn("Alice", sender.calls[1][1])
            reloaded = AlertHub(path, sender=sender)
            self.assertTrue(reloaded.settings["ntfy_enabled"] and reloaded.settings["include_person"])

    def test_push_failure_is_reported_not_raised(self):
        hub = AlertHub(sender=RecordingSender(fail=True))
        hub.update_settings({"ntfy_enabled": True, "ntfy_topic": "sentrycare-abc123"})
        hub.publish("FALL", "Hall", None)
        self.assertTrue(wait_for(lambda: hub.last_push is not None))
        self.assertFalse(hub.last_push["ok"])
        self.assertIn("unreachable", hub.last_push["error"])

    def test_settings_validation(self):
        for bad in ({"ntfy_topic": "short"}, {"ntfy_topic": "has space here"}, {"ntfy_server": "ftp://x"},
                    {"ntfy_server": "https://ntfy.sh/?x=<script>"}, {"ntfy_enabled": True}):
            with self.assertRaises(ValueError):
                validate_settings(bad, DEFAULT_SETTINGS)
        ok = validate_settings({"ntfy_server": "http://192.168.1.5:8080/", "ntfy_topic": "abc_DEF-123"}, DEFAULT_SETTINGS)
        self.assertEqual(ok["ntfy_server"], "http://192.168.1.5:8080")


class FallingStudio:
    def __init__(self):
        self.alert_next = True
    def analyze(self, frame, session_id, mode="monitor", camera=None):
        alert = {"kind": "FALL", "person": "Bob", "score": 1.5} if self.alert_next else None
        self.alert_next = False
        return {"faces": [], "fall": {"state": "CONFIRMED_FALL", "landmarks": []}, "width": frame.shape[1],
                "height": frame.shape[0], "processing_ms": 5, "alerts": [alert] if alert else []}
    def drop_session(self, session_id):
        pass


class AlertWiringTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        studio = Studio(temp.name, engine=object(), autostart=False)
        studio.status = "ready"
        self.sender = RecordingSender()
        self.hub = AlertHub(Path(temp.name) / "alert_settings.json", sender=self.sender)
        self.manager = SourceManager(FallingStudio(), start_worker=False)
        self.client = create_app(studio, sources=self.manager, alerts=self.hub).test_client()
        self.headers = {"X-SentryCare-Token": self.client.get("/api/state").json["token"]}

    def test_camera_fall_publishes_one_alert(self):
        source = self.manager.add_browser("Kitchen")
        self.manager.push(source.id, FRAME)
        self.manager.analyze_once()
        self.manager.push(source.id, FRAME)
        self.manager.analyze_once()          # still fallen: no duplicate alert
        alerts = self.client.get("/api/alerts").json["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]["camera"], alerts[0]["person"], alerts[0]["kind"]), ("Kitchen", "Bob", "FALL"))
        snap = self.client.get(f"/api/alerts/{alerts[0]['id']}/snapshot.jpg")
        self.assertEqual(snap.mimetype, "image/jpeg")
        ack = self.client.post(f"/api/alerts/{alerts[0]['id']}/ack", headers=self.headers)
        self.assertEqual(ack.json["acknowledged"]["by"], "This computer")
        self.assertEqual(self.client.post(f"/api/alerts/{alerts[0]['id']}/ack").status_code, 403)   # token needed

    def test_simulate_and_settings_are_local_only(self):
        source = self.manager.add_browser("Lounge")
        self.manager.push(source.id, FRAME)
        self.manager.analyze_once()
        sim = self.client.post("/api/alerts/test", json={"source_id": source.id}, headers=self.headers)
        self.assertEqual(sim.status_code, 201)
        self.assertEqual((sim.json["kind"], sim.json["camera"], sim.json["snapshot"]), ("TEST", "Lounge", True))
        settings = self.client.get("/api/alerts/settings").json
        self.assertFalse(settings["ntfy_enabled"])
        self.assertRegex(settings["suggested_topic"], r"^sentrycare-[0-9a-f]{12}$")
        self.assertEqual(self.client.post("/api/alerts/settings/test-push", headers=self.headers).status_code, 400)
        saved = self.client.post("/api/alerts/settings", json={"ntfy_topic": "sentrycare-abc123", "ntfy_enabled": True},
                                 headers=self.headers)
        self.assertTrue(saved.json["ntfy_enabled"])
        self.assertEqual(self.client.post("/api/alerts/settings/test-push", headers=self.headers).status_code, 200)
        self.assertEqual(len(self.sender.calls), 1)
        remote = {"environ_base": {"REMOTE_ADDR": "192.168.1.50"}}
        self.assertEqual(self.client.get("/api/alerts/settings", **remote).status_code, 403)
        self.assertEqual(self.client.post("/api/alerts/test", headers=self.headers, **remote).status_code, 403)

    def test_service_worker_served_at_root(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
        self.assertIn(b"notificationclick", response.data)
        response.close()


if __name__ == "__main__":
    unittest.main()


class StreamTests(unittest.TestCase):
    def test_stream_yields_bytes_and_unsubscribes(self):
        hub = AlertHub(sender=RecordingSender())
        q = hub.subscribe()
        stream = hub.stream(q, heartbeat=.05)
        self.assertEqual(next(stream), b"retry: 3000\n\n")
        self.assertEqual(next(stream), b": keep-alive\n\n")
        hub.publish("FALL", "Cam", None)
        self.assertTrue(next(stream).startswith(b"event: alert\n"))
        stream.close()
        self.assertNotIn(q, hub.subscribers)
