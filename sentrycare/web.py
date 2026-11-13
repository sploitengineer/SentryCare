"""Local SentryCare Studio: camera enrollment, profile gallery, and live monitoring."""

import argparse
from pathlib import Path
import re
import secrets
import threading
import time
import webbrowser

from flask import Flask, Response, jsonify, redirect, request, send_file, render_template, session
from werkzeug.exceptions import HTTPException

from .alerts import AlertHub, suggest_topic
from .lan import NetworkAccess
from .paths import DATA_DIR, PROJECT_ROOT
from .sources import SourceManager
from .studio import Studio, decode_image

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
LOOPBACK_CLIENTS = ("127.0.0.1", "::1")


def request_hostname(host):
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def create_app(studio=None, lan=None, sources=None, phone_port=8766, alerts=None):
    """Create the Studio app.

    ``lan`` enables the demo network mode, for example
    ``{"pin": "123456", "hosts": {"192.168.1.20"}, "urls": [...], "secure": True}``.
    Without it the app only accepts requests addressed to this computer's loopback interface.
    """
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static", static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024, JSON_SORT_KEYS=False)
    app.json.sort_keys = False
    service = studio or Studio(DATA_DIR / "studio")
    app.extensions["studio"] = service
    cameras = sources or SourceManager(service)
    app.extensions["sources"] = cameras
    token = secrets.token_urlsafe(32)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_SECURE=bool(lan and lan.get("secure")), SESSION_COOKIE_NAME="sentrycare")
    net = NetworkAccess(enabled=bool(lan), **{k: v for k, v in (lan or {}).items()
                                              if k in ("pin", "hosts", "urls", "secure")})
    app.extensions["network"] = net
    hub = alerts or AlertHub(service.root / "alert_settings.json",
                             click_url=lambda: net.urls[0] + "/#events" if net.enabled and net.urls else None)
    app.extensions["alerts"] = hub

    def publish_fall(camera, frame, result, info):
        hub.publish(info["kind"], camera, info["person"], {"score": info.get("score")}, frame, result)
    cameras.on_alert = publish_fall
    failures = {}

    def is_local_client():
        return request.remote_addr in LOOPBACK_CLIENTS

    def authorized():
        return is_local_client() or session.get("auth") is True

    @app.before_request
    def local_requests():
        hostname = request_hostname(request.host)
        if hostname not in LOOPBACK_HOSTS and not (net.enabled and hostname in net.hosts):
            return jsonify(error="Open Studio through localhost"), 403
        if request.endpoint == "ping":
            return None
        if net.enabled and not authorized() and request.endpoint not in ("login", "static"):
            if request.path.startswith("/api/"):
                return jsonify(error="Sign in with the demo PIN"), 401
            return redirect("/login?next=camera" if request.path == "/camera" else "/login")
        if request.endpoint == "login":
            return None
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not secrets.compare_digest(
                request.headers.get("X-SentryCare-Token", ""), token):
            return jsonify(error="Refresh the page before making changes"), 403

    @app.after_request
    def private_responses(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; "
            "connect-src 'self'; frame-ancestors 'none'")
        return response

    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, ValueError):
            return jsonify(error=str(exc)), 400
        if isinstance(exc, RuntimeError):
            return jsonify(error=str(exc)), 503
        app.logger.exception("Studio request failed")
        return jsonify(error="The operation failed. Check the local server log and try again."), 500

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        target = "/camera" if request.args.get("next") == "camera" else "/"
        if not net.enabled or authorized():
            return redirect(target)
        error = None
        if request.method == "POST":
            client = request.remote_addr or "?"
            count, until = failures.get(client, (0, 0))
            if until > time.monotonic():
                error = "Too many attempts. Wait a minute and try again."
            elif secrets.compare_digest(request.form.get("pin", "").strip().encode(), str(net.pin).encode()):
                failures.pop(client, None)
                session.clear()
                session["auth"] = True
                return redirect(target)
            else:
                count += 1
                failures[client] = (0, time.monotonic() + 60) if count >= 5 else (count, 0)
                error = "That PIN is not correct."
        return render_template("login.html", error=error), 401 if error else 200

    @app.get("/sw.js")
    def service_worker():
        response = app.send_static_file("sw.js")
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/camera")
    def camera_page():
        return render_template("camera.html")

    @app.get("/api/state")
    def state():
        return jsonify(token=token, engine={"status": service.status, "error": service.error,
                                            "body_model": service.pose_note},
                       people=service.registry.people(), settings=service.settings,
                       job=service.jobs.get(service.active_job), model="SFace · YuNet aligned faces",
                       import_available=(PROJECT_ROOT / "facial_reco_cctv.zip").is_file()
                           or any((PROJECT_ROOT / "data/known_faces").glob("*/*")))

    @app.get("/api/session")
    def session_token():
        return jsonify(token=token, engine=service.status)

    @app.get("/ping")
    def ping():
        return ("<!doctype html><meta name=viewport content='width=device-width'><title>SentryCare</title>"
                "<body style='font:20px system-ui;padding:24px;background:#f4f1ea;color:#103c36'>"
                "<h1>&#10004; Connected</h1><p>This phone can reach SentryCare. Go back and open the camera link.</p>")

    @app.get("/api/network")
    def network():
        return jsonify(lan=net.enabled, local=is_local_client(), **net.public(include_secrets=is_local_client()))

    @app.post("/api/network/enable")
    def enable_network():
        if not is_local_client():
            return jsonify(error="Phone access can only be turned on from this computer"), 403
        if net.enabled and not net.managed:
            return jsonify(error="Studio was started with --lan; phone access is already on"), 409
        app.secret_key = secrets.token_bytes(32)   # sign out phones from any earlier session
        failures.clear()
        net.start(app, phone_port)
        return jsonify(net.public(include_secrets=True))

    @app.post("/api/network/disable")
    def disable_network():
        if not is_local_client():
            return jsonify(error="Phone access can only be turned off from this computer"), 403
        if not net.managed:
            return jsonify(error="Phone access was started with --lan; stop the server with Ctrl+C"), 409
        net.stop()
        app.secret_key = secrets.token_bytes(32)
        return jsonify(net.public(include_secrets=True))

    @app.post("/api/people")
    def create_person():
        return jsonify(service.registry.create_person((request.get_json(silent=True) or {}).get("name", ""))), 201

    @app.delete("/api/people/<person_id>")
    def delete_person(person_id):
        if not service.registry.delete_person(person_id):
            return jsonify(error="Profile not found"), 404
        return jsonify(ok=True)

    @app.post("/api/people/<person_id>/samples")
    def add_sample(person_id):
        image = request.files.get("image")
        data = image.read() if image else request.get_data()
        angle = request.form.get("angle", request.args.get("angle", "front"))
        sample = service.enroll(person_id, decode_image(data), angle)
        return jsonify(sample_id=sample, person=service.registry.person(person_id)), 201

    @app.delete("/api/samples/<sample_id>")
    def delete_sample(sample_id):
        if not service.registry.delete_sample(sample_id):
            return jsonify(error="Photo not found"), 404
        return jsonify(ok=True)

    @app.get("/api/samples/<sample_id>/image")
    def sample_image(sample_id):
        if not re.fullmatch(r"[0-9a-f]{32}", sample_id):
            return jsonify(error="Photo not found"), 404
        path = service.registry.images / f"{sample_id}.jpg"
        if not path.is_file():
            return jsonify(error="Photo not found"), 404
        return send_file(path, mimetype="image/jpeg")

    @app.post("/api/train")
    def train():
        person_id = (request.get_json(silent=True) or {}).get("person_id")
        return jsonify(service.start_job("train", person_id)), 202

    @app.post("/api/import")
    def import_existing():
        return jsonify(service.start_job("import")), 202

    @app.get("/api/jobs/<job_id>")
    def job(job_id):
        if job_id not in service.jobs:
            return jsonify(error="Job not found"), 404
        return jsonify(service.jobs[job_id])

    @app.post("/api/frame")
    def frame():
        session_id = request.headers.get("X-Session-ID", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", session_id):
            raise ValueError("Invalid camera session; refresh the page")
        mode = request.args.get("mode", "monitor")
        if mode not in ("monitor", "enroll"):
            raise ValueError("Invalid camera mode")
        image = decode_image(request.get_data())
        result = service.analyze(image, session_id, mode)
        for info in (result or {}).get("alerts") or []:
            publish_fall("Laptop browser camera", image, result, info)
        return (jsonify(busy=True), 202) if result is None else jsonify(result)

    # ---- alerts ------------------------------------------------------------------------------
    def local_only(action):
        if not is_local_client():
            return jsonify(error=f"{action} is only available on the computer running SentryCare"), 403
        return None

    @app.get("/api/alerts")
    def list_alerts():
        return jsonify(alerts=hub.list())

    @app.get("/api/alerts/stream")
    def alert_stream():
        q = hub.subscribe()
        return Response(hub.stream(q), mimetype="text/event-stream",
                        headers={"X-Accel-Buffering": "no"}, direct_passthrough=True)

    @app.get("/api/alerts/<alert_id>/snapshot.jpg")
    def alert_snapshot(alert_id):
        data = hub.snapshot(alert_id)
        if data is None:
            return jsonify(error="No snapshot for this alert"), 404
        return Response(data, mimetype="image/jpeg")

    @app.post("/api/alerts/<alert_id>/ack")
    def acknowledge_alert(alert_id):
        by = "This computer" if is_local_client() else f"Device {request.remote_addr}"
        alert = hub.acknowledge(alert_id, by)
        if alert is None:
            return jsonify(error="Alert not found"), 404
        return jsonify(alert)

    @app.post("/api/alerts/test")
    def test_alert():
        denied = local_only("Simulating a fall")
        if denied:
            return denied
        source_id = (request.get_json(silent=True) or {}).get("source_id")
        frame = result = None
        camera = "Demo"
        if source_id:
            try:
                frame, result = cameras.latest(source_id)
                camera = cameras.get(source_id).name
            except KeyError:
                pass
        if result:
            result = {**result, "fall": {**result.get("fall", {}), "state": "CONFIRMED_FALL"}}
        return jsonify(hub.publish("TEST", camera, "Simulated person", {"simulated": True}, frame, result)), 201

    @app.get("/api/alerts/settings")
    def alert_settings():
        denied = local_only("Alert settings")
        return denied or jsonify({**hub.public_settings(), "suggested_topic": suggest_topic()})

    @app.post("/api/alerts/settings")
    def save_alert_settings():
        denied = local_only("Alert settings")
        return denied or jsonify(hub.update_settings(request.get_json(silent=True) or {}))

    @app.post("/api/alerts/settings/test-push")
    def test_push():
        denied = local_only("Sending a test push")
        if denied:
            return denied
        if not hub.settings["ntfy_topic"]:
            raise ValueError("Save a topic first")
        alert = {"kind": "TEST", "title": "SentryCare test", "camera": "SentryCare", "person": "",
                 "epoch": time.time()}
        result = hub._push(alert)
        return (jsonify(result), 200) if result["ok"] else (jsonify(error="Push failed: " + result["error"]), 502)

    @app.get("/api/events")
    def events():
        return jsonify(events=service.registry.events())

    # ---- camera network: phone browsers and pulled streams ---------------------------------
    def find_source(source_id):
        try:
            return cameras.get(source_id)
        except KeyError:
            return None

    @app.get("/api/sources")
    def list_sources():
        return jsonify(sources=cameras.list())

    @app.post("/api/sources")
    def add_source():
        body = request.get_json(silent=True) or {}
        if body.get("kind") == "browser":
            source = cameras.add_browser(body.get("name"))
        elif body.get("kind") == "stream":
            if not is_local_client():
                return jsonify(error="Add stream cameras from the computer running Studio"), 403
            source = cameras.add_stream(body.get("name"), body.get("url"))
        else:
            raise ValueError("Choose a browser or stream camera")
        return jsonify(source.public()), 201

    @app.delete("/api/sources/<source_id>")
    def remove_source(source_id):
        if not cameras.remove(source_id):
            return jsonify(error="Camera not found"), 404
        return jsonify(ok=True)

    @app.post("/api/sources/<source_id>/frame")
    def push_frame(source_id):
        if find_source(source_id) is None:
            return jsonify(error="Camera was removed; reconnect it"), 404
        return jsonify(cameras.push(source_id, decode_image(request.get_data())))

    @app.get("/api/sources/<source_id>/snapshot.jpg")
    def source_snapshot(source_id):
        if find_source(source_id) is None:
            return jsonify(error="Camera not found"), 404
        data = cameras.snapshot(source_id)
        if data is None:
            return jsonify(error="No video yet"), 404
        return Response(data, mimetype="image/jpeg")

    @app.get("/api/sources/<source_id>/stream")
    def source_stream(source_id):
        if find_source(source_id) is None:
            return jsonify(error="Camera not found"), 404
        return Response(cameras.mjpeg(source_id), mimetype="multipart/x-mixed-replace; boundary=frame",
                        direct_passthrough=True)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true", help="Open the local UI after starting")
    parser.add_argument("--lan", action="store_true",
                        help="Demo network mode: HTTPS on this Wi-Fi with a PIN so phones can connect")
    parser.add_argument("--pin", help="Demo PIN for --lan (default: random 6 digits)")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535")
    if not args.lan:
        print(f"SentryCare Studio: http://127.0.0.1:{args.port}", flush=True)
        app = create_app()
        host, ssl_context, local_url = "127.0.0.1", None, f"http://127.0.0.1:{args.port}"
    else:
        from .netinfo import ensure_certificate, lan_addresses
        pin = args.pin or f"{secrets.randbelow(10**6):06d}"
        if not re.fullmatch(r"\d{4,12}", pin):
            parser.error("--pin must be 4-12 digits")
        addresses = lan_addresses()
        if not addresses:
            parser.error("No Wi-Fi/LAN address found; connect to a network first")
        ssl_context = tuple(str(p) for p in ensure_certificate(DATA_DIR / "tls", addresses))
        urls = [f"https://{a}:{args.port}" for a in addresses]
        app = create_app(lan={"pin": pin, "hosts": set(addresses), "urls": urls, "secure": True})
        host, local_url = "0.0.0.0", f"https://127.0.0.1:{args.port}"
        print("\nSentryCare Studio - demo network mode (HTTPS, self-signed certificate)", flush=True)
        print(f"  This computer : {local_url}", flush=True)
        for url in urls:
            print(f"  Same Wi-Fi    : {url}    phone camera: {url}/camera", flush=True)
        print(f"  Demo PIN      : {pin}", flush=True)
        print("  Phones show a certificate warning once: choose Advanced -> Proceed.", flush=True)
        print("  Use only on a trusted network. Ctrl+C stops the server.\n", flush=True)
    if args.open_browser:
        opener = threading.Timer(1, lambda: webbrowser.open(local_url))
        opener.daemon = True
        opener.start()
    app.run(host=host, port=args.port, threaded=True, use_reloader=False, debug=False, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
