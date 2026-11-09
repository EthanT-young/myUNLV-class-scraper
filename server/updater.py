"""Keeps the data directory in sync with a GitHub release asset.

publish.sh on the scraping machine uploads site/data/*.json as a tarball to a
release (default tag "data-latest"). This checks the release every few
minutes using an ETag (unchanged checks are cheap and don't count against
GitHub's rate limit) and installs a new tarball only after every file in it
parses.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("updater")

NAME_RE = re.compile(r"[\w-]+\.json")
MAX_UNPACKED = 64 * 1024 * 1024
MAX_BYTES = 100 * 1024 * 1024
API = "https://api.github.com"


class DataUpdater:
    def __init__(self, data_dir: Path, repo: str, tag: str = "data-latest", asset: str = "data.tar.gz",
                 token: str | None = None, interval: float = 300, on_update=None):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"bad repo name {repo!r}")
        self.data_dir, self.repo, self.tag, self.asset = data_dir, repo, tag, asset
        self.token, self.interval, self.on_update = token, interval, on_update
        self.etag = None
        self.installed_id = None
        self.state_file = data_dir / ".release.json"
        try:
            self.installed_id = json.loads(self.state_file.read_text()).get("asset_id")
        except (OSError, ValueError):
            pass
        self.status = {"lastCheck": None, "lastUpdate": None, "lastError": None}
        self._stop = threading.Event()

    def _request(self, url: str, accept: str, etag: str | None = None):
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "unlv-class-finder"})
        if etag:
            req.add_header("If-None-Match", etag)
        if self.token:
            # Not forwarded on the redirect to GitHub's download host.
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(req, timeout=60)

    def check(self) -> bool:
        """Install the release's tarball if it changed. Returns True if updated."""
        self.status["lastCheck"] = time.time()
        url = f"{API}/repos/{self.repo}/releases/tags/{self.tag}"
        try:
            # Without data, always fetch in full so a failed install is retried.
            etag = self.etag if (self.data_dir / "terms.json").exists() else None
            with self._request(url, "application/vnd.github+json", etag) as r:
                new_etag = r.headers.get("ETag")
                release = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return False
            raise
        asset = next((a for a in release.get("assets", []) if a.get("name") == self.asset), None)
        if not asset:
            raise RuntimeError(f"release {self.tag} has no asset {self.asset}")
        if asset["id"] == self.installed_id and (self.data_dir / "terms.json").exists():
            self.etag = new_etag
            return False
        if asset.get("size", 0) > MAX_BYTES:
            raise RuntimeError("asset too large")
        with self._request(f"{API}/repos/{self.repo}/releases/assets/{asset['id']}",
                           "application/octet-stream") as r:
            blob = r.read(MAX_BYTES + 1)
        if len(blob) > MAX_BYTES:
            raise RuntimeError("asset too large")
        self.install(blob)
        self.installed_id = asset["id"]
        self.state_file.write_text(json.dumps({"asset_id": asset["id"], "updated": asset.get("updated_at")}))
        self.status["lastUpdate"] = time.time()
        self.etag = new_etag  # only after a successful install, so failures are retried
        log.info("installed data from %s (%s)", self.tag, asset.get("updated_at"))
        return True

    def install(self, blob: bytes):
        """Validate every file, then move them into place (terms.json last)."""
        files: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            members = tar.getmembers()
            if sum(m.size for m in members if m.isfile()) > MAX_UNPACKED:
                raise RuntimeError("tarball unpacks too large")
            for m in members:
                name = m.name.removeprefix("./")
                if not m.isfile() or not NAME_RE.fullmatch(name):
                    continue  # ignore anything that isn't a plain top-level .json file
                files[name] = tar.extractfile(m).read()
        if "terms.json" not in files:
            raise RuntimeError("tarball has no terms.json")
        terms = json.loads(files["terms.json"])["terms"]
        for t in terms:
            name = f"{t['term']}.json"
            if not re.fullmatch(r"\d{4}\.json", name) or name not in files:
                raise RuntimeError(f"terms.json lists {name}, which is missing or invalid")
            if not isinstance(json.loads(files[name]).get("courses"), list):
                raise RuntimeError(f"{name} has no course list")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(files, key=lambda n: n == "terms.json"):
            tmp = self.data_dir / f".{name}.tmp"
            tmp.write_bytes(files[name])
            os.replace(tmp, self.data_dir / name)
        if self.on_update:
            self.on_update()

    def run(self):
        while not self._stop.is_set():
            try:
                self.check()
                self.status["lastError"] = None
            except Exception as e:  # network blips, GitHub outages, bad uploads
                log.warning("data update failed: %s", e)
                self.status["lastError"] = f"{type(e).__name__}: {e}"[:300]
            self._stop.wait(self.interval)

    def start(self):
        threading.Thread(target=self.run, name="data-updater", daemon=True).start()

    def stop(self):
        self._stop.set()
