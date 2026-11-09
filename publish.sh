#!/usr/bin/env bash
# Scrape on this machine, then publish the data as a GitHub release asset.
# The server checks that release every few minutes and installs new data itself,
# so data never goes into git history.
#
#   ./publish.sh                          # every term on offer
#   ./publish.sh --term 2272              # scraper options are passed through
#   ./publish.sh --skip-scrape            # upload the data you already have
#   ./publish.sh --allow-failures ...     # publish even if some subjects failed
#
# Needs the GitHub CLI, logged in once:  gh auth login
set -euo pipefail
cd "$(dirname "$0")"

TAG=${DATA_RELEASE:-data-latest}
allow_failures=0 skip_scrape=0
args=()
for a in "$@"; do
  case $a in
    --allow-failures) allow_failures=1 ;;
    --skip-scrape) skip_scrape=1 ;;
    *) args+=("$a") ;;
  esac
done
[[ " ${args[*]-} " == *" --workers "* ]] || args+=(--workers 6)

command -v gh >/dev/null || { echo "Install the GitHub CLI first: https://cli.github.com" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Log in first: gh auth login" >&2; exit 1; }

if (( ! skip_scrape )); then
  echo "==> Scraping: python3 -m scraper ${args[*]}"
  set +e
  python3 -m scraper "${args[@]}"
  status=$?
  set -e
  if (( status != 0 )); then
    if (( allow_failures )); then
      echo "!! Scraper reported failures (exit $status); publishing anyway." >&2
    else
      echo "!! Scraper reported failures (exit $status); not publishing. Use --allow-failures to override." >&2
      exit "$status"
    fi
  fi
fi

echo "==> Checking the scraper"
python3 -m unittest discover -s tests -t . -q

echo "==> Packaging site/data"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
summary=$(python3 - <<'EOF'
import json, re, sys
from pathlib import Path
terms = json.load(open("site/data/terms.json"))["terms"]
for t in terms:  # every listed term must exist and parse
    assert re.fullmatch(r"\d{4}", t["term"]), t
    json.load(open(f"site/data/{t['term']}.json"))
print(", ".join(f"{t['name']}: {t['sections']} sections" for t in terms))
EOF
)
files=(terms.json)
while IFS= read -r term; do files+=("$term.json"); done < <(python3 -c '
import json; [print(t["term"]) for t in json.load(open("site/data/terms.json"))["terms"]]')
tar -czf "$tmp/data.tar.gz" -C site/data "${files[@]}"
echo "    $(du -h "$tmp/data.tar.gz" | cut -f1)  ($summary)"

echo "==> Uploading to release $TAG"
notes="Class data used by the website. Updated $(date -u '+%Y-%m-%d %H:%M UTC'): $summary"
if ! gh release view "$TAG" >/dev/null 2>&1; then
  gh release create "$TAG" --title "Class data" --notes "$notes" --latest=false
fi
gh release upload "$TAG" "$tmp/data.tar.gz" --clobber
gh release edit "$TAG" --notes "$notes" >/dev/null
echo "==> Published. The server installs it within about 5 minutes."
