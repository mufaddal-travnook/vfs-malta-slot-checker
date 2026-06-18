#!/usr/bin/env bash
#
# EC2 entrypoint for the VFS Malta slot checker — invoke this from cron hourly.
#
# It wraps the self-healing supervisor with:
#   * flock    — a lockfile so overlapping runs can't pile up (if one hour's run
#                hangs past the hour, the next cron tick is skipped, not stacked).
#   * xvfb-run — a fresh virtual X display per run (real/headed Chrome needs a
#                display; -a auto-picks a free display number) torn down after.
#
# The supervisor itself launches & kills Chrome and retries on failure, so this
# script stays thin.
#
# Crontab (hourly):
#   0 * * * * /opt/vfs-malta-slot-checker/run_ec2.sh >> /opt/vfs-malta-slot-checker/app.log 2>&1
#
# Prerequisites on the box:
#   sudo apt update
#   sudo apt install -y xvfb google-chrome-stable   # or chromium
#   python3 -m venv .venv && . .venv/bin/activate
#   pip install -r requirements.txt
#   python -m playwright install chromium            # Playwright client deps
#
set -euo pipefail

# Resolve the project dir (this script's location) so cron's CWD doesn't matter.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer the venv python if present, else system python3.
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python3"
fi

LOCKFILE="/tmp/vfs-slot-checker.lock"

# flock: take an exclusive, non-blocking lock on FD 200. If another run holds it,
# exit immediately (skip this tick) rather than stacking a second browser.
exec 200>"$LOCKFILE"
if ! flock -n 200; then
  echo "[$(date '+%F %T')] Previous run still in progress — skipping this tick."
  exit 0
fi

echo "[$(date '+%F %T')] Starting VFS slot-check run (xvfb + supervisor)..."

# xvfb-run -a : fresh auto-numbered virtual display for headed Chrome.
# Pass a reasonable screen size so the page renders at a desktop viewport.
xvfb-run -a --server-args="-screen 0 1280x1024x24" \
  "$PYTHON" -m src.supervisor "$@"

echo "[$(date '+%F %T')] Run finished."
