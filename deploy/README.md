# Deploying on the server

These steps set up the site at `https://<your domain>/projects/unlvEnrollmentViewer/` on an
Oracle Cloud Ampere (ARM) instance that already runs Caddy in Docker.

The container serves the site and a small API for live seat counts. It downloads the
scraped data from this repo's `data-latest` GitHub release by itself, checking every
5 minutes, so the server never scrapes and never needs `git pull` for new data.

## What the server does and doesn't do

- **It does:** serve the site, check seat counts for the sections visitors are looking at
  (at most 2 UNLV sessions, 1 request per second each), and download new data releases.
- **It doesn't:** scrape the full catalog. That happens on the scraping PC with `./publish.sh`.
- **Resources:** capped at half a CPU core and 256 MB of memory in `compose.yaml`. Expect
  well under that.

## First-time setup

1. **Find Caddy's Docker network.** The container joins it so Caddy can reach it by name.

   ```bash
   docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' <caddy-container-name>
   ```

   If the network is called `caddy`, there is nothing to change. Otherwise, put its name in a
   `.env` file next to `compose.yaml`:

   ```bash
   echo "CADDY_NETWORK=<network name>" >> .env
   ```

2. **Clone the repo and start the container.**

   ```bash
   git clone https://github.com/EthanT-young/myUNLV-class-scraper.git
   cd myUNLV-class-scraper
   docker compose up -d --build
   ```

   If the repo is private, also add `GITHUB_TOKEN=<token with read access to the repo>` to
   `.env` so the container can download release assets. A fine-grained token with
   "Contents: read" on this one repo is enough.

3. **Add the route to Caddy.** Copy the lines in `deploy/Caddyfile.snippet` into your site
   block, then reload Caddy:

   ```bash
   docker exec <caddy-container-name> caddy reload --config /etc/caddy/Caddyfile
   ```

   The redirect line matters. The page loads `data/` and `api/` with relative URLs, so it
   must be served with a trailing slash.

4. **Check it.**

   ```bash
   docker compose ps                                   # should say "healthy" after ~30 s
   docker compose logs -f                              # look for "installed data from data-latest"
   curl -s https://<domain>/projects/unlvEnrollmentViewer/api/health
   ```

   In the health response, `terms` lists the loaded terms and `data.lastError` shows any
   problem downloading the release. Until the first `./publish.sh` run, the release doesn't
   exist, so expect a 404 there and an empty term list.

## Updating

- **New class data:** nothing to do. The container installs a new release within 5 minutes.
- **New code:** pull and rebuild. This takes under a minute; the data volume is kept.

  ```bash
  git pull && docker compose up -d --build
  ```

## Tuning (in `compose.yaml`)

| Setting | Default | Notes |
| --- | --- | --- |
| `SEAT_WORKERS` | 2 | Parallel UNLV sessions for live seats (max 4). More workers refresh more sections per minute. |
| `SEAT_DELAY` | 1.0 | Seconds between requests per session (min 0.5). |
| `SEAT_TTL` | 120 | How often a section on someone's screen is re-checked, in seconds. |
| `RATE_PER_MIN` | 30 | API requests per minute per visitor IP. |
| `DATA_POLL` | 300 | Seconds between checks for a new data release. |

Each refresh costs about 1 request per course plus 2 per section on screen, so 2 workers
keep roughly 100 to 150 sections up to date at once. Sections everyone is viewing are shared,
and the most-viewed ones are refreshed first.

## Troubleshooting

- **502 from Caddy:** the container isn't on Caddy's network, or isn't running.
  Check `docker network inspect <network>` and `docker compose ps`.
- **Page loads but no classes:** no data yet. Check `data.lastError` in `/api/health`, and
  make sure `./publish.sh` has run at least once.
- **Seats say "Unavailable right now":** check `lastError` in `/api/health` and
  `docker compose logs`. UNLV's site may be down, or slow during registration.
