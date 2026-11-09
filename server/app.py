"""HTTP server: the static site plus a small JSON API for live seat counts.

Meant to run behind Caddy, which handles HTTPS, compression and the URL
prefix. Uses only the standard library so the container stays small.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .seats import Catalog, SeatTracker

log = logging.getLogger("app")

MAX_CLASSES = 80
CLASS_RE = re.compile(r"\d{1,6}")
TERM_RE = re.compile(r"\d{4}")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class RateLimiter:
    """Token bucket per client IP."""

    def __init__(self, rate_per_min: float = 30, burst: int = 10, clock=time.monotonic):
        self.rate = rate_per_min / 60
        self.burst = burst
        self.clock = clock
        self.lock = threading.Lock()
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, ip: str) -> bool:
        now = self.clock()
        with self.lock:
            tokens, last = self.buckets.get(ip, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            ok = tokens >= 1
            self.buckets[ip] = (tokens - 1 if ok else tokens, now)
            if len(self.buckets) > 10000:  # drop idle clients
                cutoff = now - 600
                self.buckets = {k: v for k, v in self.buckets.items() if v[1] > cutoff}
            return ok


class Handler(SimpleHTTPRequestHandler):
    server_version = "unlv-class-finder"
    sys_version = ""
    tracker: SeatTracker
    catalog: Catalog
    limiter: RateLimiter
    data_dir: Path
    updater = None

    # -- helpers -------------------------------------------------------------
    def client_ip(self) -> str:
        # Caddy sets X-Forwarded-For; the right-most entry is the one it added.
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[-1].strip() if fwd else self.client_address[0]

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, status: int, body: dict):
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_message(self, fmt, *args):
        if os.environ.get("ACCESS_LOG") == "1":
            log.info("%s %s", self.client_ip(), fmt % args)

    # -- routing -------------------------------------------------------------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/seats":
            return self.api_seats()
        if path == "/api/health":
            body = {"ok": True, "terms": self.catalog.terms, **self.tracker.status()}
            if self.updater:
                body["data"] = self.updater.status
            return self.send_json(200, body)
        if path.startswith("/api/"):
            return self.send_json(404, {"error": "not found"})
        # Static files: no directory listings, no dotfiles.
        if any(part.startswith(".") for part in path.split("/")):
            return self.send_error(HTTPStatus.NOT_FOUND)
        return super().do_GET()

    def do_HEAD(self):
        if urlsplit(self.path).path.startswith("/api/"):
            return self.send_json(405, {"error": "use GET"})
        return super().do_HEAD()

    def translate_path(self, path):
        # /data/<name>.json comes from the data directory (kept up to date by the
        # updater); everything else from the site directory.
        p = urlsplit(path).path
        if p.startswith("/data/"):
            name = p[len("/data/"):]
            if re.fullmatch(r"[\w-]+\.json", name):
                return str(self.data_dir / name)
            return str(self.data_dir / ".missing")
        return super().translate_path(path)

    def list_directory(self, path):
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def api_seats(self):
        if not self.limiter.allow(self.client_ip()):
            self.send_response(429)
            self.send_header("Retry-After", "10")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        q = parse_qs(urlsplit(self.path).query)
        term = (q.get("term") or [""])[0]
        raw = (q.get("classes") or [""])[0]
        if not TERM_RE.fullmatch(term) or term not in self.catalog.terms:
            return self.send_json(400, {"error": "unknown term"})
        nbrs = list(dict.fromkeys(n for n in raw.split(",") if CLASS_RE.fullmatch(n)))
        if not nbrs or len(nbrs) > MAX_CLASSES:
            return self.send_json(400, {"error": f"send 1 to {MAX_CLASSES} class numbers"})
        known = self.tracker.want(term, nbrs)
        self.send_json(200, {
            "term": term,
            "now": time.time(),
            "ttl": self.tracker.ttl,
            "seats": self.tracker.get(term, known),
        })


def make_server(site_dir: Path, host: str, port: int, tracker: SeatTracker, catalog: Catalog,
                limiter: RateLimiter | None = None, data_dir: Path | None = None,
                updater=None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {
        "tracker": tracker, "catalog": catalog, "limiter": limiter or RateLimiter(),
        "data_dir": data_dir or site_dir / "data", "updater": updater,
    })

    def factory(*args, **kwargs):
        return handler(*args, directory=str(site_dir), **kwargs)

    server = ThreadingHTTPServer((host, port), factory)
    server.daemon_threads = True
    return server
