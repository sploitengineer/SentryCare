"""On-demand phone access: a second HTTPS listener on the LAN, started from the Studio UI.

The normal Studio server stays on http://127.0.0.1. When the user turns on phone access from this
computer, the same Flask app is also served over HTTPS on all interfaces with a fresh PIN, so phones
on the same Wi-Fi/hotspot can open the camera page. Turning it off stops that listener.
"""

import io
import secrets
import threading

from .paths import DATA_DIR


def qr_svg(text):
    """Return an SVG QR code as a data URI, or None when segno is not installed."""
    try:
        import segno
    except ImportError:
        return None
    buffer = io.BytesIO()
    segno.make(text, error="m").save(buffer, kind="svg", scale=6, border=2, dark="#103c36", light="#ffffff")
    import base64
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()


class NetworkAccess:
    """Mutable network-mode settings shared with the request guards in ``web.create_app``."""

    def __init__(self, enabled=False, pin=None, hosts=(), urls=(), secure=True, port=None, managed=False):
        self.enabled = enabled
        self.pin = pin
        self.hosts = set(hosts)
        self.urls = list(urls)
        self.secure = secure
        self.port = port
        self.managed = managed          # True when started from the UI (can be stopped there)
        self.error = None
        self.server = None
        self.thread = None
        self.lock = threading.Lock()

    def public(self, include_secrets):
        data = {"enabled": self.enabled, "managed": self.managed, "port": self.port, "error": self.error,
                "urls": self.urls if self.enabled else [],
                "camera_urls": [u + "/camera" for u in self.urls] if self.enabled else [],
                "ping_urls": [u + "/ping" for u in self.urls] if self.enabled else []}
        if include_secrets and self.enabled:
            data["pin"] = self.pin
            data["qr"] = qr_svg(data["camera_urls"][0]) if data["camera_urls"] else None
        return data

    def start(self, app, port=8766, addresses=None):
        """Serve ``app`` over HTTPS on all interfaces. Returns once the socket is listening."""
        from werkzeug.serving import make_server
        from .netinfo import ensure_certificate, lan_addresses
        with self.lock:
            if self.enabled:
                return
            addresses = addresses if addresses is not None else lan_addresses()
            if not addresses:
                self.error = "No Wi-Fi or hotspot address found. Connect this computer to a network first."
                raise RuntimeError(self.error)
            cert, key = ensure_certificate(DATA_DIR / "tls", addresses)
            try:
                server = make_server("0.0.0.0", port, app, threaded=True, ssl_context=(str(cert), str(key)))
            except OSError as exc:
                self.error = f"Port {port} is busy ({exc.strerror or exc}). Close the other program or restart Studio."
                raise RuntimeError(self.error) from exc
            self.server, self.port, self.managed, self.secure, self.error = server, port, True, True, None
            self.pin = f"{secrets.randbelow(10**6):06d}"
            self.hosts = set(addresses)
            self.urls = [f"https://{a}:{port}" for a in addresses]
            app.config["SESSION_COOKIE_SECURE"] = True
            self.thread = threading.Thread(target=server.serve_forever, daemon=True, name="phone-access")
            self.thread.start()
            self.enabled = True

    def stop(self):
        with self.lock:
            if not self.managed or not self.server:
                return False
            self.enabled = False
            self.server.shutdown()
            self.server.server_close()
            self.server = self.thread = None
            self.hosts, self.urls, self.pin = set(), [], None
            return True
