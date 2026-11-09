"""Run the site and seat API:  python3 -m server

Settings come from environment variables (see compose.yaml):
  SITE_DIR      directory with index.html              (default: ./site)
  DATA_DIR      directory with terms.json + term files (default: $SITE_DIR/data)
  DATA_REPO     GitHub "owner/repo" whose release holds the data; enables auto-updates
  DATA_RELEASE  release tag                            (default: data-latest)
  DATA_POLL     seconds between release checks         (default: 300)
  GITHUB_TOKEN  only needed if the repo is private
  HOST, PORT    listen address                         (default: 0.0.0.0:8080)
  SEAT_WORKERS  parallel PeopleSoft sessions           (default: 2)
  SEAT_DELAY    seconds between requests per session  (default: 1.0)
  SEAT_TTL      seconds before a section is re-checked (default: 120)
  RATE_PER_MIN  API requests per minute per client IP  (default: 30)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .app import RateLimiter, make_server
from .seats import Catalog, SeatTracker
from .updater import DataUpdater


def env(name, default, cast=str):
    try:
        return cast(os.environ.get(name, default))
    except ValueError:
        sys.exit(f"Invalid value for {name}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    site = Path(env("SITE_DIR", "site")).resolve()
    if not (site / "index.html").exists():
        sys.exit(f"{site}/index.html not found; set SITE_DIR")
    workers = max(1, min(4, env("SEAT_WORKERS", 2, int)))
    delay = max(0.5, env("SEAT_DELAY", 1.0, float))

    data_dir = Path(env("DATA_DIR", str(site / "data"))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(data_dir)
    updater = None
    if os.environ.get("DATA_REPO"):
        updater = DataUpdater(data_dir, os.environ["DATA_REPO"], tag=env("DATA_RELEASE", "data-latest"),
                              token=os.environ.get("GITHUB_TOKEN") or None,
                              interval=max(60.0, env("DATA_POLL", 300, float)), on_update=catalog.refresh)
        updater.start()
    catalog.refresh()
    tracker = SeatTracker(catalog, workers=workers, delay=delay, ttl=max(30.0, env("SEAT_TTL", 120, float)))
    tracker.start()

    stop = threading.Event()

    def housekeeping():  # pick up new data after a git pull; forget old demand
        while not stop.wait(30):
            catalog.refresh()
            tracker.prune()

    threading.Thread(target=housekeeping, daemon=True).start()

    server = make_server(site, env("HOST", "0.0.0.0"), env("PORT", 8080, int), tracker, catalog,
                         RateLimiter(rate_per_min=env("RATE_PER_MIN", 30, float)),
                         data_dir=data_dir, updater=updater)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    logging.getLogger("app").info("serving %s on %s:%s with %d seat workers",
                                  site, *server.server_address, workers)
    try:
        server.serve_forever()
    finally:
        stop.set()
        tracker.stop()
        if updater:
            updater.stop()


if __name__ == "__main__":
    main()
